"""Staff -> Reviewer -> Admin hand-off: Staff picks the reviewer, the reviewer forwards to Admin."""
import pytest
from conftest import new_app, get_row, uid, make_user


def submitted(staff, **kw):
    pk = new_app(staff, **kw)
    assert staff.post(f"/application/{pk}/status", data={"status": "Submitted"}).status_code == 302
    assert get_row(pk).status == "Submitted"
    return pk


def test_staff_assigns_reviewer(staff, reviewer, appm):
    pk = submitted(staff, cnic="4210100000101")
    assert "Reviewer ko bhejein" in staff.get(f"/application/{pk}").get_data(as_text=True)
    r = staff.post(f"/application/{pk}/send-to-reviewer", data={"reviewer_id": uid("reviewer1")})
    assert r.status_code == 302
    a = get_row(pk)
    assert a.reviewer_id == uid("reviewer1") and a.status == "Pending Review"
    with appm.app.app_context():
        assert appm.AuditLog.query.filter_by(application_id=pk, action="application.reviewer_by_staff").count() == 1
    # the reviewer now sees it
    assert f"/application/{pk}" in reviewer.get("/reviewer").get_data(as_text=True)


def test_staff_cannot_assign_others_or_after_review_started(staff, staff2, reviewer, admin):
    pk = submitted(staff, cnic="4210100000102")
    # another staff member is not the owner
    assert staff2.post(f"/application/{pk}/send-to-reviewer",
                       data={"reviewer_id": uid("reviewer1")}).status_code == 403
    # only real Reviewers may be chosen
    staff.post(f"/application/{pk}/send-to-reviewer", data={"reviewer_id": uid("admin")})
    assert get_row(pk).reviewer_id is None
    staff.post(f"/application/{pk}/send-to-reviewer", data={"reviewer_id": uid("reviewer1")})
    reviewer.post(f"/application/{pk}/status", data={"status": "In Review"})
    assert get_row(pk).status == "In Review"
    assert staff.post(f"/application/{pk}/send-to-reviewer",
                      data={"reviewer_id": uid("reviewer1")}).status_code == 403
    # a draft cannot be sent either
    draft = new_app(staff, cnic="4210100000103")
    assert staff.post(f"/application/{draft}/send-to-reviewer",
                      data={"reviewer_id": uid("reviewer1")}).status_code == 403


def test_reviewer_forwards_to_admin_and_admin_decides(staff, reviewer, admin, appm):
    pk = submitted(staff, cnic="4210100000104")
    staff.post(f"/application/{pk}/send-to-reviewer", data={"reviewer_id": uid("reviewer1")})
    assert "Admin ko forward karein" in reviewer.get(f"/application/{pk}").get_data(as_text=True)
    r = reviewer.post(f"/application/{pk}/forward", data={"admin_id": uid("admin"), "note": "Sab theek hai"})
    assert r.status_code == 302
    a = get_row(pk)
    assert a.status == "In Review" and a.forwarded_by == uid("reviewer1") and a.forwarded_to == uid("admin")
    assert a.forwarded_at is not None
    page = admin.get("/admin").get_data(as_text=True)
    assert "Reviewer ne faisle ke liye bheji" in page and f"/application/{pk}" in page
    with appm.app.app_context():
        assert appm.ReviewNote.query.filter_by(application_id=pk, kind="recommend").first().body == "Sab theek hai"
    # staff cannot forward
    assert staff.post(f"/application/{pk}/forward", data={}).status_code == 403
    # Admin approves: the hand-off mark is cleared, the audit log keeps it
    assert admin.post(f"/application/{pk}/status", data={"status": "Approved"}).status_code == 302
    a = get_row(pk)
    assert a.status == "Approved" and a.forwarded_at is None
    with appm.app.app_context():
        assert appm.AuditLog.query.filter_by(application_id=pk, action="application.forwarded").count() == 1


def test_other_reviewer_cannot_forward(staff, reviewer, reviewer2):
    pk = submitted(staff, cnic="4210100000105")
    staff.post(f"/application/{pk}/send-to-reviewer", data={"reviewer_id": uid("reviewer1")})
    assert reviewer2.post(f"/application/{pk}/forward", data={}).status_code in (403, 404)
    assert get_row(pk).forwarded_at is None


def test_permissions_can_be_switched_off(admin, staff, reviewer, appm):
    pk = submitted(staff, cnic="4210100000106")
    admin.post("/admin/permissions", data={"Staff": sorted(appm.DEFAULT_ROLE_PERMS["Staff"] - {"app.assign_reviewer"}),
                                           "Reviewer": sorted(appm.DEFAULT_ROLE_PERMS["Reviewer"])})
    try:
        assert staff.post(f"/application/{pk}/send-to-reviewer",
                          data={"reviewer_id": uid("reviewer1")}).status_code == 403
    finally:
        admin.post("/admin/permissions", data={"Staff": sorted(appm.DEFAULT_ROLE_PERMS["Staff"]),
                                               "Reviewer": sorted(appm.DEFAULT_ROLE_PERMS["Reviewer"])})


# ------------------------------------------------------------------ database rules (PostgreSQL)
def as_user(pg, user_id, role):
    pg.execute("begin")
    pg.execute("set local role tooba_app")
    pg.execute("select set_config('app.user_id', %s, true), set_config('app.role', %s, true)", (str(user_id), role))


def test_db_function_checks_owner(pg, staff, staff2, reviewer):
    pk = submitted(staff, cnic="4210100000107")
    as_user(pg, uid("staff2"), "Staff")
    try:
        with pytest.raises(Exception, match="not found"):
            pg.execute("select staff_assign_reviewer(%s, %s)", (pk, uid("reviewer1")))
    finally:
        pg.execute("rollback")
    as_user(pg, uid("staff1"), "Staff")
    try:
        assert pg.execute("select staff_assign_reviewer(%s, %s)", (pk, uid("reviewer1"))).fetchone()[0] == "Pending Review"
        # the role is back to Staff afterwards, so a direct update is still refused by RLS / guards
        assert pg.execute("select current_setting('app.role')").fetchone()[0] == "Staff"
    finally:
        pg.execute("rollback")


def test_db_refuses_forward_without_permission(pg, staff, reviewer):
    pk = submitted(staff, cnic="4210100000108")
    staff.post(f"/application/{pk}/send-to-reviewer", data={"reviewer_id": uid("reviewer1")})
    as_user(pg, uid("reviewer1"), "Reviewer")
    try:
        with pytest.raises(Exception, match="Not allowed to forward"):
            pg.execute("update application set forwarded_at = now(), forwarded_by = %s where id = %s",
                       (uid("staff1"), pk))
    finally:
        pg.execute("rollback")
