"""Phases 5–9: CNIC documents page, submit rules, disbursement sheets, cheques, closing,
Excel export, Google Sheets sync (with a fake Google client) and reports."""
import io
import json
import os
import pytest
from conftest import (PG, DBNAME, new_app, get_row, uid, make_user, upload_kind, complete_app, REQUIRED_CNIC)


def text_of(r):
    return r.get_data(as_text=True)


def approve(admin, staff, reviewer, pk, loan_no):
    staff.post(f"/application/{pk}/loan-number", data={"loan_number": loan_no})
    assert staff.post(f"/application/{pk}/status", data={"status": "Submitted"}).status_code == 302
    admin.post(f"/application/{pk}/assign", data={"reviewer_id": uid("reviewer1")})
    reviewer.post(f"/application/{pk}/status", data={"status": "In Review"})
    reviewer.post(f"/application/{pk}/status", data={"status": "Approved"})
    assert get_row(pk).status == "Approved", get_row(pk).status


_numbers = iter(range(154, 999))


def new_batch(admin, date="2026-11-08"):
    n = next(_numbers)
    r = admin.post("/disbursement", data={"number": n, "disb_date": date})
    assert r.status_code == 302, r.status_code
    return int(r.headers["Location"].rstrip("/").split("/")[-1]), n


def entry_of(appm, pk):
    with appm.app.app_context():
        e = appm.DisbursementEntry.query.filter_by(application_id=pk, removed_at=None).first()
        return e.id if e else None


# ------------------------------------------------------------------ CNIC documents + submit rules
def test_submit_needs_cnic_purpose_fee(staff):
    pk = new_app(staff, complete=False, cnic="4210111100001")
    r = staff.post(f"/application/{pk}/status", data={"status": "Submitted"}, follow_redirects=True)
    page = text_of(r)
    assert get_row(pk).status == "Draft"
    assert "Applicant CNIC — Front Side" in page and "Loan purpose" in page
    complete_app(staff, pk)
    staff.post(f"/application/{pk}/status", data={"status": "Submitted"})
    assert get_row(pk).status == "Submitted"


def test_family_cnic_optional_until_reviewer_requires(admin, staff, reviewer, appm):
    pk = new_app(staff, cnic="4210111100002")
    page = text_of(staff.get(f"/application/{pk}/print"))
    payload = json.loads(page.split('id="formPayload">')[1].split("</script>")[0])
    assert [r["title"] for r in payload["cnic"]] == ["APPLICANT CNIC", "FIRST WITNESS CNIC", "SECOND WITNESS CNIC"]
    assert all(r["front"] and r["back"] for r in payload["cnic"])
    assert 'id="cnicPage"' in page and "cnicpage.js" in page
    # only the assigned reviewer (or Admin) decides
    assert staff.post(f"/application/{pk}/family-cnic", data={"required": "1"}).status_code == 403
    admin.post(f"/application/{pk}/assign", data={"reviewer_id": uid("reviewer1")})
    reviewer.post(f"/application/{pk}/family-cnic", data={"required": "1"})
    assert get_row(pk).family_cnic_required is True
    payload = json.loads(text_of(staff.get(f"/application/{pk}/print")).split('id="formPayload">')[1].split("</script>")[0])
    assert payload["cnic"][-1]["title"] == "FAMILY MEMBER CNIC" and payload["cnic"][-1]["front"] is None
    staff.post(f"/application/{pk}/status", data={"status": "Submitted"})
    assert get_row(pk).status == "Draft"                     # family CNIC now mandatory
    upload_kind(staff, pk, "cnic_fam_front")
    upload_kind(staff, pk, "cnic_fam_back")
    staff.post(f"/application/{pk}/status", data={"status": "Submitted"})
    assert get_row(pk).status == "Submitted"


def test_cnic_slots_accept_images_only_and_keep_history(staff, appm):
    pk = new_app(staff, cnic="4210111100003")
    r = staff.post(f"/application/{pk}/upload", data={"kind": "cnic_app_front", "file": (io.BytesIO(b"%PDF-1"), "x.pdf")},
                   content_type="multipart/form-data", follow_redirects=True)
    assert "JPG, PNG ya WEBP" in text_of(r)
    upload_kind(staff, pk, "cnic_app_front", b"\xff\xd8second")
    with appm.app.app_context():
        rows = appm.Document.query.filter_by(application_id=pk, kind="cnic_app_front").order_by(appm.Document.id).all()
        assert len(rows) == 2 and rows[0].replaced_by == rows[1].id


def test_fee_follows_amount_and_history_is_kept(admin, staff, appm):
    pk = new_app(staff, complete=False, cnic="4210111100004", amount="40000")
    a = get_row(pk)
    assert int(a.loan_amount) == 40000 and int(a.fee_amount) == 1500
    # Admin changes the slab's fee later: this application keeps 1500
    with appm.app.app_context():
        f = appm.FeeCategory.query.filter(appm.FeeCategory.loan_amount == 40000).first()
        fid, name = f.id, f.name
    admin.post(f"/admin/settings/fee/{fid}", data={"name": name, "loan_amount": "40000", "fee_amount": "1600", "active": "1"})
    assert int(get_row(pk).fee_amount) == 1500
    admin.post(f"/admin/settings/fee/{fid}", data={"name": name, "loan_amount": "40000", "fee_amount": "1500", "active": "1"})


def test_money_change_after_approval_needs_rereview(admin, staff, reviewer, appm):
    pk = new_app(staff, cnic="4210111100005")
    approve(admin, staff, reviewer, pk, "RR-1")
    with appm.app.app_context():
        edu = appm.PurposeCategory.query.filter_by(name="Education").first().id
    admin.post(f"/application/{pk}/details", data={"purpose_id": edu})
    a = get_row(pk)
    assert a.status == "Submitted Again" and a.approved_by is None and a.purpose_label == "Education"


# ------------------------------------------------------------------ disbursement sheets
def test_sheet_flow(admin, staff, reviewer, appm):
    p1 = new_app(staff, name="Sheet One", cnic="4210122200001")
    p2 = new_app(staff, name="Sheet Two", cnic="4210122200002")
    p3 = new_app(staff, name="Not Approved", cnic="4210122200003")
    approve(admin, staff, reviewer, p1, "5582")
    approve(admin, staff, reviewer, p2, "5583")
    bid, number = new_batch(admin)
    page = text_of(admin.get(f"/disbursement/{bid}"))
    assert "Sheet One" in page and "Not Approved" not in page            # only approved are offered
    admin.post(f"/disbursement/{bid}/add", data={"app_id": [p1, p2, p3]})
    assert get_row(p1).status == get_row(p2).status == "Added to Disbursement Sheet"
    assert get_row(p3).status == "Draft" and entry_of(appm, p3) is None
    # no duplicates: a second sheet cannot take the same application
    bid2, _ = new_batch(admin)
    admin.post(f"/disbursement/{bid2}/add", data={"app_id": [p1]})
    with appm.app.app_context():
        assert appm.DisbursementEntry.query.filter_by(application_id=p1, removed_at=None).count() == 1
    page = text_of(admin.get(f"/disbursement/{bid}"))
    for txt in (f"{number}th", "Sheet One", "42101-2220000-1", "50,000", "1,700", "100,000", "3,400",
                "Disbursement Report", "President", "General Secretary", "Enquiry Officer", "Business"):
        assert txt in page, txt
    # remove needs a reason and puts the application back
    e2 = entry_of(appm, p2)
    admin.post(f"/disbursement/entry/{e2}/remove", data={"reason": ""})
    assert entry_of(appm, p2) == e2
    admin.post(f"/disbursement/entry/{e2}/remove", data={"reason": "Applicant did not come"})
    assert entry_of(appm, p2) is None and get_row(p2).status == "Ready for Disbursement"
    # disbursed
    e1 = entry_of(appm, p1)
    admin.post(f"/disbursement/entry/{e1}/disbursed")
    assert get_row(p1).status == "Disbursed"
    admin.post(f"/disbursement/entry/{e1}/remove", data={"reason": "x"})
    assert entry_of(appm, p1) == e1                                          # paid rows stay
    with appm.app.app_context():
        acts = {h.action for h in appm.AuditLog.query.filter_by(application_id=p1)}
    assert {"disbursement.added", "disbursement.disbursed"} <= acts


def test_cheques(admin, staff, reviewer, staff2, appm):
    p1 = new_app(staff, name="Cheque One", cnic="4210133300001")
    p2 = new_app(staff, name="Cheque Two", cnic="4210133300002")
    approve(admin, staff, reviewer, p1, "6001")
    approve(admin, staff, reviewer, p2, "6002")
    bid, _ = new_batch(admin)
    admin.post(f"/disbursement/{bid}/add", data={"app_id": [p1, p2]})
    e1, e2 = entry_of(appm, p1), entry_of(appm, p2)
    # reviewer enters cheque numbers (cheque.manage); staff cannot
    assert staff.post(f"/disbursement/entry/{e1}/cheque", data={"cheque_no": "2080318014"}).status_code == 403
    assert staff.get(f"/disbursement/{bid}").status_code == 403
    assert "Disbursement Sheets" in text_of(reviewer.get("/reviewer"))
    reviewer.post(f"/disbursement/entry/{e1}/cheque", data={"cheque_no": "2080318014", "status": "Issued"})
    r = reviewer.post(f"/disbursement/entry/{e2}/cheque", data={"cheque_no": "2080318014", "status": "Issued"},
                      follow_redirects=True)
    assert "pehle se" in text_of(r)                                          # duplicate active number
    # changing a number needs a reason; old cheque is cancelled, history kept
    reviewer.post(f"/disbursement/entry/{e1}/cheque", data={"cheque_no": "2080318099", "status": "Issued"})
    reviewer.post(f"/disbursement/entry/{e1}/cheque", data={"cheque_no": "2080318099", "status": "Issued",
                                                            "reason": "Printing mistake"})
    with appm.app.app_context():
        rows = appm.Cheque.query.filter_by(entry_id=e1).order_by(appm.Cheque.id).all()
        assert [(c.cheque_no, c.status) for c in rows] == [("2080318014", "Cancelled"), ("2080318099", "Issued")]
        assert rows[0].cancel_reason == "Printing mistake"
    # the cancelled number can be used again elsewhere
    reviewer.post(f"/disbursement/entry/{e2}/cheque", data={"cheque_no": "2080318014", "status": "Issued"})
    reviewer.post(f"/disbursement/entry/{e2}/cheque", data={"cheque_no": "2080318014", "status": "Cleared"})
    with appm.app.app_context():
        assert appm.Cheque.query.filter_by(entry_id=e2, status="Cleared").count() == 1
    assert "2080318099" in text_of(reviewer.get(f"/disbursement/{bid}"))


def test_close_and_reopen(admin, staff, reviewer, appm):
    pk = new_app(staff, name="Closing", cnic="4210144400001")
    approve(admin, staff, reviewer, pk, "7001")
    bid, _ = new_batch(admin)
    admin.post(f"/disbursement/{bid}/add", data={"app_id": [pk]})
    e = entry_of(appm, pk)
    assert reviewer.post(f"/disbursement/{bid}/close").status_code == 403    # period.close is Admin-only
    admin.post(f"/disbursement/{bid}/close")
    r = reviewer.post(f"/disbursement/entry/{e}/cheque", data={"cheque_no": "9990001", "status": "Issued"},
                      follow_redirects=True)
    assert "close hai" in text_of(r)
    admin.post(f"/disbursement/{bid}/reopen", data={"reason": ""})
    with appm.app.app_context():
        assert appm.db.session.get(appm.DisbursementBatch, bid).status == "closed"
    admin.post(f"/disbursement/{bid}/reopen", data={"reason": "Cheque number missing"})
    with appm.app.app_context():
        b = appm.db.session.get(appm.DisbursementBatch, bid)
        assert b.status == "open" and b.reopen_reason == "Cheque number missing"
        acts = [h.action for h in appm.AuditLog.query.filter_by(entity="disbursement_batch", entity_id=bid)]
    assert "disbursement.closed" in acts and "disbursement.reopened" in acts


def test_closed_sheet_enforced_in_database(pg, admin, staff, reviewer, appm):
    pk = new_app(staff, name="DB Closed", cnic="4210144400002")
    approve(admin, staff, reviewer, pk, "7002")
    bid, _ = new_batch(admin)
    admin.post(f"/disbursement/{bid}/add", data={"app_id": [pk]})
    admin.post(f"/disbursement/{bid}/close")
    e = entry_of(appm, pk)
    with pytest.raises(Exception, match="closed"):
        pg.execute("update disbursement_entry set amount = 1 where id = %s", (e,))
    with pytest.raises(Exception, match="closed"):
        pg.execute("insert into cheque(entry_id, cheque_no) values (%s, 'X1')", (e,))
    with pytest.raises(Exception):
        pg.execute("insert into disbursement_entry(batch_id, application_id) values (%s, %s)", (bid, pk))


def test_excel_export(admin, staff, reviewer, appm):
    from openpyxl import load_workbook
    pk = new_app(staff, name="Excel Person", cnic="4210155500001")
    upload_kind(staff, pk, "photo")
    approve(admin, staff, reviewer, pk, "8001")
    bid, number = new_batch(admin)
    admin.post(f"/disbursement/{bid}/add", data={"app_id": [pk]})
    r = reviewer.get(f"/disbursement/{bid}/xlsx")
    assert r.status_code == 200
    ws = load_workbook(io.BytesIO(r.data)).active
    values = [c for row in ws.iter_rows(values_only=True) for c in row if c is not None]
    for v in ("THE TOOBA FOUNDATION", f"Disbursement Number {number}th", "Excel Person", "42101-5550000-1",
              "8001", "Disbursement Report", "President", "Enquiry Officer"):
        assert v in values, v
    assert ws.page_setup.orientation == "landscape"


# ------------------------------------------------------------------ Google Sheets (fake client)
class FakeGoogle:
    def __init__(self, fail=False):
        self.created, self.writes, self.fail = [], {}, fail

    def find_year(self, year):
        return None

    def create_year(self, year):
        if self.fail:
            raise RuntimeError("Google is down")
        self.created.append(year)
        return f"sheet{year}", f"https://docs.google.com/spreadsheets/d/sheet{year}", {"November": 11, "Yearly Summary": 99}

    def write_tab(self, sid, tab, sheet_id, rows, bold_rows=()):
        if self.fail:
            raise RuntimeError("Google is down")
        self.writes[(sid, tab)] = rows


def test_google_sync(admin, staff, reviewer, appm, monkeypatch):
    os.environ["CRON_SECRET"] = "cron-test"
    fake = FakeGoogle(fail=True)
    monkeypatch.setattr(appm, "GOOGLE_CLIENT", fake)
    pk = new_app(staff, name="Google Person", cnic="4210166600001")
    approve(admin, staff, reviewer, pk, "9001")
    bid, number = new_batch(admin, date="2026-11-20")
    # Google is down: the website still works, the failure is logged and kept for retry
    assert admin.post(f"/disbursement/{bid}/add", data={"app_id": [pk]}).status_code == 302
    assert entry_of(appm, pk) is not None
    with appm.app.app_context():
        assert appm.GsheetSync.query.filter_by(year=2026, month=11, dirty=True).count() == 1
        assert appm.GsheetSyncLog.query.filter_by(ok=False).count() >= 1
    # Google is back: the daily cron retries
    fake.fail = False
    assert appm.app.test_client().get("/cron/gsheets").status_code == 401
    r = appm.app.test_client().get("/cron/gsheets", headers={"Authorization": "Bearer cron-test"})
    assert r.status_code == 200 and r.get_json()["synced"] >= 2
    rows = fake.writes[("sheet2026", "November")]
    assert sum(1 for row in rows if "Google Person" in row) == 1
    # a cheque change rewrites the tab — still exactly one row, and no second spreadsheet
    e = entry_of(appm, pk)
    reviewer.post(f"/disbursement/entry/{e}/cheque", data={"cheque_no": "55501", "status": "Issued"})
    rows = fake.writes[("sheet2026", "November")]
    assert sum(1 for row in rows if "Google Person" in row) == 1
    assert any("55501" in row for row in rows)
    assert fake.created == [2026]
    assert ("sheet2026", "Yearly Summary") in fake.writes
    page = text_of(admin.get("/admin/google-sheets"))
    assert "sheet2026" in page and "Failed" in page and "OK" in page
    assert "Google Sheet" in text_of(reviewer.get(f"/disbursement/{bid}"))


def test_google_page_admin_only(staff, reviewer):
    for c in (staff, reviewer):
        assert c.get("/admin/google-sheets").status_code == 403


# ------------------------------------------------------------------ reports
def test_reports(admin, staff, reviewer, appm):
    r = admin.get("/admin/reports?year=2026")
    page = text_of(r)
    assert r.status_code == 200
    for h in ("Monthly applications", "Monthly fee collection", "Staff-wise", "Reviewer-wise", "Purpose-wise",
              "Fee-wise collection", "Saalana"):
        assert h in page, h
    for kind in ("monthly", "yearly", "staff", "reviewers", "purpose", "fees"):
        csv = admin.get(f"/admin/reports/export?kind={kind}&year=2026")
        assert csv.status_code == 200 and csv.mimetype == "text/csv"
    assert staff.get("/admin/reports").status_code == 403
    data = None
    with appm.app.test_request_context():
        appm.g._db_actor = (str(uid("admin")), "Admin")       # the database only shows rows to a known role
        data = appm.report_data(2026)
        appm.db.session.remove()
    assert data["totals"]["fees"] >= 1700 and any(r["name"] == "Business" for r in data["purpose"])


def test_admin_can_delete_open_sheet(admin, staff, reviewer, appm, monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(appm, "GOOGLE_CLIENT", fake)
    pk = new_app(staff, name="Delete Me", cnic="4210177700001")
    approve(admin, staff, reviewer, pk, "DEL-1")
    bid, number = new_batch(admin, date="2026-11-21")
    admin.post(f"/disbursement/{bid}/add", data={"app_id": [pk]})
    e = entry_of(appm, pk)
    reviewer.post(f"/disbursement/entry/{e}/cheque", data={"cheque_no": "DEL-CHQ-1", "status": "Issued"})
    assert any("Delete Me" in row for row in fake.writes[("sheet2026", "November")])
    # only Admin, and only with a reason
    assert reviewer.post(f"/disbursement/{bid}/delete", data={"reason": "x"}).status_code == 403
    admin.post(f"/disbursement/{bid}/delete", data={"reason": ""})
    assert entry_of(appm, pk) == e
    admin.post(f"/disbursement/{bid}/delete", data={"reason": "Test sheet by mistake"})
    with appm.app.app_context():
        b = appm.db.session.get(appm.DisbursementBatch, bid)
        assert b.deleted_at and b.delete_reason == "Test sheet by mistake"
        assert appm.Cheque.query.filter_by(entry_id=e).one().status == "Cancelled"
        assert appm.AuditLog.query.filter_by(action="disbursement.deleted", entity_id=bid).count() == 1
    assert entry_of(appm, pk) is None and get_row(pk).status == "Ready for Disbursement"
    assert admin.get(f"/disbursement/{bid}").status_code == 404
    assert f">{number}<" not in text_of(admin.get("/disbursement"))
    assert not any("Delete Me" in row for row in fake.writes[("sheet2026", "November")])
    # the application can go on a new sheet, and the cheque number can be used again
    bid2, _ = new_batch(admin, date="2026-11-22")
    admin.post(f"/disbursement/{bid2}/add", data={"app_id": [pk]})
    assert get_row(pk).status == "Added to Disbursement Sheet"
    reviewer.post(f"/disbursement/entry/{entry_of(appm, pk)}/cheque", data={"cheque_no": "DEL-CHQ-1", "status": "Issued"})
    with appm.app.app_context():
        assert appm.Cheque.query.filter_by(cheque_no="DEL-CHQ-1", status="Issued").count() == 1


def test_paid_or_closed_sheet_cannot_be_deleted(admin, staff, reviewer, appm):
    pk = new_app(staff, name="Paid Keep", cnic="4210177700002")
    approve(admin, staff, reviewer, pk, "DEL-2")
    bid, _ = new_batch(admin, date="2026-11-23")
    admin.post(f"/disbursement/{bid}/add", data={"app_id": [pk]})
    admin.post(f"/disbursement/entry/{entry_of(appm, pk)}/disbursed")
    r = admin.post(f"/disbursement/{bid}/delete", data={"reason": "try"}, follow_redirects=True)
    assert "delete nahi ho sakti" in text_of(r)
    admin.post(f"/disbursement/{bid}/close")
    r = admin.post(f"/disbursement/{bid}/delete", data={"reason": "try"}, follow_redirects=True)
    assert "delete nahi ho sakti" in text_of(r)
    with appm.app.app_context():
        assert appm.db.session.get(appm.DisbursementBatch, bid).deleted_at is None


def test_database_refuses_deleting_closed_sheet(pg, admin, appm):
    bid, _ = new_batch(admin, date="2026-11-24")
    admin.post(f"/disbursement/{bid}/close")
    with pytest.raises(Exception, match="Only an open sheet"):
        pg.execute("update disbursement_batch set deleted_at = now(), delete_reason = 'x' where id = %s", (bid,))
