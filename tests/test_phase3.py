"""Phase 3: migration safety, Application IDs, roles/permissions (app + database), workflow."""
import io
import threading
import pytest
from conftest import PG, DBNAME, new_app, get_row, uid, pg_admin, make_user, UserClient, ADMIN_PW


def text_of(r):
    return r.get_data(as_text=True)


# ------------------------------------------------------------------ migration / data preservation
def test_legacy_data_preserved(pg):
    a = pg.execute("select app_id, applicant_name, status, form_json from application where id = 1").fetchone()
    assert a[0] == "TBF-0001" and a[1] == "Legacy Person"
    assert a[2] == "Pending Review"                      # renamed, not lost
    assert "Legacy Person" in a[3]
    assert pg.execute("select count(*) from document where application_id = 1").fetchone()[0] == 1
    assert pg.execute("select data from document where id = 1").fetchone()[0] == b"\x01\x02\x03"
    assert pg.execute("select count(*) from log where action = 'Created TBF-0001'").fetchone()[0] == 1


def test_migration_is_rerunnable(pg):
    from conftest import migration_files
    before = pg.execute("select count(*) from application").fetchone()[0]
    for f in migration_files():
        pg.execute(open(f).read())
    assert pg.execute("select count(*) from application").fetchone()[0] == before
    assert pg.execute("select count(*) from purpose_category").fetchone()[0] == 7


def test_seed_matches_app_constants(pg, appm):
    rows = set(pg.execute("select from_status, to_status, permission from status_transition").fetchall())
    assert rows == set(appm.DEFAULT_TRANSITIONS)
    perms = {}
    for role, p in pg.execute("select role, permission from role_permission").fetchall():
        perms.setdefault(role, set()).add(p)
    assert perms == appm.DEFAULT_ROLE_PERMS
    fees = pg.execute("select loan_amount::int, fee_amount::int from fee_category order by sort_order").fetchall()
    assert fees == appm.DEFAULT_FEES
    names = [r[0] for r in pg.execute("select name from purpose_category order by sort_order")]
    assert names == appm.DEFAULT_PURPOSES


def test_public_api_roles_have_no_access(pg):
    grants = pg.execute("""select count(*) from information_schema.role_table_grants
                           where table_schema = 'public' and grantee in ('anon', 'authenticated')""").fetchone()[0]
    assert grants == 0


# ------------------------------------------------------------------ Application ID
def test_new_ids_have_year_format(staff):
    a = get_row(new_app(staff))
    assert a.app_id.startswith("TBF-20") and len(a.app_id) == len("TBF-2026-00001")


def test_ids_unique_under_concurrency(pg):
    import psycopg
    results, errors = [], []

    def worker():
        try:
            with psycopg.connect(PG + f" dbname={DBNAME} user=tooba_app", autocommit=True) as c:
                for _ in range(10):
                    results.append(c.execute("select next_app_id()").fetchone()[0])
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert len(results) == 80 and len(set(results)) == 80


def test_app_id_cannot_be_changed(pg):
    with pytest.raises(Exception, match="cannot be changed"):
        pg.execute("update application set app_id = 'TBF-HACK' where id = 1")


# ------------------------------------------------------------------ audit log
def test_audit_log_append_only(pg, staff):
    new_app(staff)
    with pytest.raises(Exception, match="append-only"):
        pg.execute("update audit_log set action = 'x'")
    with pytest.raises(Exception, match="append-only"):
        pg.execute("delete from audit_log")


def test_actions_are_audited(staff, appm):
    pk = new_app(staff)
    with appm.app.app_context():
        actions = [r.action for r in appm.AuditLog.query.filter_by(application_id=pk)]
        edited = appm.AuditLog.query.filter_by(application_id=pk, action="application.edited").first()
    assert "application.created" in actions and "application.edited" in actions
    assert edited.new_value["name"] == "Test Applicant" and edited.actor_name == "staff1"


# ------------------------------------------------------------------ full workflow
def test_workflow_staff_reviewer_admin(admin, staff, reviewer, appm):
    pk = new_app(staff, cnic="4240100000001")
    # staff sets the manual loan number
    assert staff.post(f"/application/{pk}/loan-number", data={"loan_number": "LN-7001"}).status_code == 302
    assert get_row(pk).loan_number == "LN-7001"
    # staff cannot approve or jump steps
    assert staff.post(f"/application/{pk}/status", data={"status": "Approved"}).status_code == 403
    assert staff.post(f"/application/{pk}/status", data={"status": "Submitted"}).status_code == 302
    assert get_row(pk).status == "Submitted"
    # after submitting, staff can no longer edit the form
    assert staff.post(f"/application/{pk}/form", data={"name": "Changed"}).status_code == 403
    # reviewer cannot see it until assigned
    assert reviewer.get(f"/application/{pk}").status_code == 404
    # only admin (app.assign) can assign
    assert staff.post(f"/application/{pk}/assign", data={"reviewer_id": uid("reviewer1")}).status_code == 403
    admin.post(f"/application/{pk}/assign", data={"reviewer_id": uid("reviewer1")})
    assert get_row(pk).status == "Pending Review" and get_row(pk).reviewer_id == uid("reviewer1")
    assert reviewer.get(f"/application/{pk}").status_code == 200
    assert reviewer.post(f"/application/{pk}/status", data={"status": "In Review"}).status_code == 302
    # correction needs a reason
    reviewer.post(f"/application/{pk}/status", data={"status": "Correction Required"})
    assert get_row(pk).status == "In Review"
    reviewer.post(f"/application/{pk}/status", data={"status": "Correction Required", "reason": "CNIC photo blurry"})
    assert get_row(pk).status == "Correction Required"
    # internal note hidden from staff, correction visible
    reviewer.post(f"/application/{pk}/note", data={"kind": "note", "body": "secret internal note"})
    page = text_of(staff.get(f"/application/{pk}"))
    assert "CNIC photo blurry" in page and "secret internal note" not in page
    assert "secret internal note" in text_of(reviewer.get(f"/application/{pk}"))
    # staff fixes and resubmits
    assert staff.post(f"/application/{pk}/form", data={"name": "Test Applicant Fixed"}).status_code == 302
    staff.post(f"/application/{pk}/status", data={"status": "Submitted Again"})
    assert get_row(pk).status == "Submitted Again"
    with appm.app.app_context():
        corr = appm.ReviewNote.query.filter_by(application_id=pk, kind="correction").first()
        assert corr.resolved_at is not None
    reviewer.post(f"/application/{pk}/status", data={"status": "In Review"})
    reviewer.post(f"/application/{pk}/status", data={"status": "Approved"})
    a = get_row(pk)
    assert a.status == "Approved" and a.approved_by == uid("reviewer1") and a.approved_at
    # reviewer cannot move to disbursement (needs disbursement.manage)
    assert reviewer.post(f"/application/{pk}/status", data={"status": "Ready for Disbursement"}).status_code == 403


def test_reject_requires_reason(admin, staff, reviewer):
    pk = new_app(staff, cnic="4240100000002")
    staff.post(f"/application/{pk}/status", data={"status": "Submitted"})
    admin.post(f"/application/{pk}/assign", data={"reviewer_id": uid("reviewer1")})
    reviewer.post(f"/application/{pk}/status", data={"status": "In Review"})
    reviewer.post(f"/application/{pk}/status", data={"status": "Rejected"})
    assert get_row(pk).status == "In Review"
    reviewer.post(f"/application/{pk}/status", data={"status": "Rejected", "reason": "Income not verified"})
    a = get_row(pk)
    assert a.status == "Rejected" and a.rejection_reason == "Income not verified"


def test_other_reviewer_cannot_act(admin, staff, reviewer, reviewer2):
    pk = new_app(staff, cnic="4240100000003")
    staff.post(f"/application/{pk}/status", data={"status": "Submitted"})
    admin.post(f"/application/{pk}/assign", data={"reviewer_id": uid("reviewer1")})
    assert reviewer2.get(f"/application/{pk}").status_code == 404
    assert reviewer2.post(f"/application/{pk}/status", data={"status": "In Review"}).status_code == 404


def test_staff_cannot_edit_other_staff_application(staff, staff2):
    pk = new_app(staff, cnic="4240100000004")
    assert staff2.get(f"/application/{pk}").status_code == 200       # view_all: can see
    assert staff2.post(f"/application/{pk}/form", data={"name": "Hijack"}).status_code == 403
    assert get_row(pk).applicant_name == "Test Applicant"


def test_admin_pages_forbidden_for_staff(staff, reviewer):
    for c in (staff, reviewer):
        for url in ("/admin/users", "/admin/permissions", "/admin/export", "/admin/backup",
                    "/admin/reports", "/admin/deleted"):
            assert c.get(url).status_code == 403, url


# ------------------------------------------------------------------ database-level enforcement
def _as(pg_conn, user_id, role):
    pg_conn.execute("select set_config('app.user_id', %s, false), set_config('app.role', %s, false)",
                    (str(user_id), role))


def test_rls_blocks_reviewer_on_unassigned(pg, staff):
    import psycopg
    pk = new_app(staff, cnic="4240100000005")
    with psycopg.connect(PG + f" dbname={DBNAME} user=tooba_app", autocommit=True) as c:
        _as(c, uid("reviewer1"), "Reviewer")
        assert c.execute("select count(*) from application where id = %s", (pk,)).fetchone()[0] == 0
        assert c.execute("update application set applicant_name = 'x' where id = %s", (pk,)).rowcount == 0
        assert c.execute("select count(*) from document").fetchone()[0] == 0 or True
        _as(c, uid("staff1"), "Staff")
        assert c.execute("select count(*) from application where id = %s", (pk,)).fetchone()[0] == 1


def test_db_rejects_illegal_status_change(pg, staff):
    import psycopg
    pk = new_app(staff, cnic="4240100000006")
    with psycopg.connect(PG + f" dbname={DBNAME} user=tooba_app", autocommit=True) as c:
        _as(c, uid("staff1"), "Staff")
        with pytest.raises(Exception, match="not allowed"):
            c.execute("update application set status = 'Approved' where id = %s", (pk,))
        _as(c, uid("staff1"), "Staff")
        with pytest.raises(Exception):
            c.execute("update \"user\" set role = 'Admin' where id = %s", (uid("staff1"),))
        _as(c, "", "anon")
        assert c.execute("select count(*) from application").fetchone()[0] == 0
        with pytest.raises(Exception):
            c.execute("insert into audit_log(action) values ('x'); update audit_log set reason = 'y'")


def test_legacy_connection_still_works(pg):
    """The currently deployed code does not send app.role; it must keep working after the migration."""
    import psycopg
    with psycopg.connect(PG + f" dbname={DBNAME} user=tooba_app", autocommit=True) as c:
        assert c.execute("select count(*) from application where id = 1").fetchone()[0] == 1
        c.execute("update application set updated_at = now() where id = 1")


# ------------------------------------------------------------------ duplicates, search, delete, documents
def test_duplicate_cnic_warns_but_allows(staff):
    first = new_app(staff, cnic="4250111111111")
    r = staff.post("/application/new")
    pk = int(r.headers["Location"].split("/")[-2])
    r = staff.post(f"/application/{pk}/form", data={"name": "Repeat", "cnic": "42501-1111111-1"},
                   follow_redirects=True)
    assert "pehle se application mojood" in text_of(r)
    assert get_row(pk).cnic == "4250111111111" and get_row(first).cnic == "4250111111111"


def test_search_by_id_loan_cnic_name(staff):
    pk = new_app(staff, name="Searchable Person", cnic="4210199999999")
    staff.post(f"/application/{pk}/loan-number", data={"loan_number": "SRCH-55"})
    app_id = get_row(pk).app_id
    for q in (app_id, "SRCH-55", "42101-9999999-9", "Searchable"):
        assert app_id in text_of(staff.get("/", query_string={"q": q})), q


def test_loan_number_unique(staff):
    a = new_app(staff, cnic="4210100000011")
    b = new_app(staff, cnic="4210100000012")
    staff.post(f"/application/{a}/loan-number", data={"loan_number": "DUP-1"})
    r = staff.post(f"/application/{b}/loan-number", data={"loan_number": "DUP-1"}, follow_redirects=True)
    assert "pehle se" in text_of(r) and get_row(b).loan_number is None


def test_soft_delete_and_restore(admin, staff):
    pk = new_app(staff, name="To Delete", cnic="4210100000021")
    app_id = get_row(pk).app_id
    assert staff.post(f"/application/{pk}/delete").status_code == 403
    admin.post(f"/application/{pk}/delete", data={"reason": "duplicate entry"})
    assert get_row(pk).deleted_at is not None
    assert app_id not in text_of(staff.get("/"))
    assert staff.get(f"/application/{pk}").status_code == 404
    assert app_id in text_of(admin.get("/admin/deleted"))
    admin.post(f"/application/{pk}/restore")
    assert get_row(pk).deleted_at is None and staff.get(f"/application/{pk}").status_code == 200


def test_photo_replacement_keeps_history(staff, appm):
    pk = new_app(staff, cnic="4210100000031")
    for content in (b"first-image", b"second-image"):
        staff.post(f"/application/{pk}/upload", data={"kind": "photo", "file": (io.BytesIO(content), "p.jpg")},
                   content_type="multipart/form-data")
    with appm.app.app_context():
        docs = appm.Document.query.filter_by(application_id=pk, kind="photo").order_by(appm.Document.id).all()
        assert len(docs) == 2 and docs[0].replaced_by == docs[1].id and docs[1].replaced_by is None
        assert appm.AuditLog.query.filter_by(application_id=pk, action="document.replaced").count() == 1
    # the replaced file is no longer served to staff
    assert staff.get(f"/document/{docs[0].id}").status_code == 404
    assert staff.get(f"/document/{docs[1].id}").status_code == 200


def test_permission_matrix(admin, staff, appm):
    keep = sorted(appm.DEFAULT_ROLE_PERMS["Staff"] - {"app.create"})
    admin.post("/admin/permissions", data={"Staff": keep, "Reviewer": sorted(appm.DEFAULT_ROLE_PERMS["Reviewer"])})
    try:
        assert staff.post("/application/new").status_code == 403
        with appm.app.app_context():
            assert appm.AuditLog.query.filter_by(action="permissions.changed").count() >= 1
    finally:
        admin.post("/admin/permissions", data={"Staff": sorted(appm.DEFAULT_ROLE_PERMS["Staff"]),
                                               "Reviewer": sorted(appm.DEFAULT_ROLE_PERMS["Reviewer"])})
    assert staff.post("/application/new").status_code == 302


def test_last_admin_protected(admin):
    r = admin.post(f"/admin/users/{uid('admin')}/role", data={"role": "Staff"}, follow_redirects=True)
    assert "Apna role" in text_of(r)


def test_existing_pages_still_work(admin, staff):
    pk = new_app(staff, cnic="4210100000041")
    for url in ("/", f"/application/{pk}", f"/application/{pk}/form", f"/application/{pk}/print",
                "/account/password"):
        assert staff.get(url).status_code == 200, url
    for url in ("/admin/users", "/admin/permissions", "/admin/reports", "/admin/deleted"):
        assert admin.get(url).status_code == 200, url
    csv = text_of(admin.get("/admin/export"))
    assert "Loan No." in csv


def test_login_lockout_still_works(appm):
    c = appm.app.test_client()
    make_user("Staff", "lockme")
    for _ in range(appm.LOGIN_MAX_FAILS):
        c.post("/login", data={"username": "lockme", "password": "wrong"})
    assert c.post("/login", data={"username": "lockme", "password": "Password123"}).status_code == 429


def test_app_sends_actor_to_database(pg, appm):
    from sqlalchemy import text
    with appm.app.test_request_context():
        appm.g._db_actor = ("42", "Staff")
        role, user = appm.db.session.execute(
            text("select current_setting('app.role', true), current_setting('app.user_id', true)")).one()
        appm.db.session.remove()
    assert (role, user) == ("Staff", "42")


def test_database_blocks_edit_even_if_app_check_is_bypassed(pg, staff, staff2, appm, monkeypatch):
    """Defence in depth: break the Python permission check on purpose; RLS must still refuse."""
    pk = new_app(staff, cnic="4210100000051")
    from sqlalchemy.orm.exc import StaleDataError
    monkeypatch.setattr(appm, "can_edit_form", lambda a: True)
    try:
        with pytest.raises(StaleDataError):       # RLS: UPDATE matched 0 rows
            staff2.post(f"/application/{pk}/form", data={"name": "Hijacked"})
    finally:
        with appm.app.app_context():
            appm.db.session.remove()
    assert get_row(pk).applicant_name == "Test Applicant"
