"""Separate Admin / Reviewer / Staff sections: login pages, dashboards, menus, URL areas."""
import pytest
from conftest import UserClient, ADMIN_PW, make_user, uid, new_app


def text_of(r):
    return r.get_data(as_text=True)


def test_login_page_shows_three_sections(appm):
    page = text_of(appm.app.test_client().get("/login"))
    for label, url in (("Admin Login", "/login/admin"), ("Reviewer Login", "/login/reviewer"),
                       ("Staff Login", "/login/staff")):
        assert label in page and url in page
    for portal, title in (("admin", "Admin Login"), ("reviewer", "Reviewer Login"), ("staff", "Staff Login")):
        assert title in text_of(appm.app.test_client().get(f"/login/{portal}"))
    assert appm.app.test_client().get("/login/superuser").status_code == 404


@pytest.mark.parametrize("role,portal,home", [("Admin", "admin", "/admin"), ("Reviewer", "reviewer", "/reviewer"),
                                              ("Staff", "staff", "/staff")])
def test_each_login_redirects_to_own_dashboard(appm, role, portal, home):
    name = "admin" if role == "Admin" else f"sec_{portal}"
    pw = ADMIN_PW if role == "Admin" else "Password123"
    if role != "Admin":
        make_user(role, name)
    c = appm.app.test_client()
    r = c.post(f"/login/{portal}", data={"username": name, "password": pw})
    assert r.status_code == 302 and r.headers["Location"].endswith(home)
    assert c.get("/").headers["Location"].endswith(home)          # old address goes to own section
    assert c.get(home).status_code == 200


def test_wrong_section_login_is_refused(appm, staff, reviewer):
    for user, pw, wrong in (("staff1", "Password123", "admin"), ("staff1", "Password123", "reviewer"),
                            ("reviewer1", "Password123", "staff"), ("reviewer1", "Password123", "admin"),
                            ("admin", ADMIN_PW, "staff")):
        c = appm.app.test_client()
        r = c.post(f"/login/{wrong}", data={"username": user, "password": pw})
        assert r.status_code == 403, (user, wrong)
        assert c.get("/staff").status_code == 302          # not logged in


def test_direct_url_access_blocked(admin, staff, reviewer):
    for c, own, others in ((staff, "/staff", ["/admin", "/admin/users", "/admin/audit", "/admin/settings",
                                              "/reviewer", "/reviewer/applications"]),
                           (reviewer, "/reviewer", ["/admin", "/admin/applications", "/admin/permissions",
                                                    "/staff", "/staff/applications"]),
                           (admin, "/admin", ["/staff", "/staff/applications", "/reviewer"])):
        assert c.get(own).status_code == 200
        for url in others:
            assert c.get(url).status_code == 403, (c.username, url)


def test_logged_out_section_urls_go_to_that_login(appm):
    c = appm.app.test_client()
    for area in ("admin", "reviewer", "staff"):
        r = c.get(f"/{area}")
        assert r.status_code == 302 and r.headers["Location"].endswith(f"/login/{area}")


def test_menus_are_separate(admin, staff, reviewer):
    a, s, r = text_of(admin.get("/admin")), text_of(staff.get("/staff")), text_of(reviewer.get("/reviewer"))
    assert "Admin Panel" in a and "Audit Log" in a and "Staff &amp; Reviewers" in a
    for page in (s, r):
        assert "Audit Log" not in page and "/admin/" not in page and "Permissions" not in page
    assert "Staff Desk" in s and "Reviewer Desk" not in s and "My Applications" in s
    assert "Reviewer Desk" in r and "Staff Desk" not in r and "New Application" not in r


def test_role_change_ends_session(admin, appm):
    c = make_user("Staff", "rolechange")
    assert c.get("/staff").status_code == 200
    admin.post(f"/admin/users/{uid('rolechange')}/role", data={"role": "Reviewer"})
    r = c.get("/staff")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert UserClient("rolechange", "Password123", "reviewer").get("/reviewer").status_code == 200


def test_dashboards_show_right_work(admin, staff, reviewer):
    pk = new_app(staff, name="Dash Person", cnic="4210177777777")
    assert "Dash Person" in text_of(staff.get("/staff"))
    staff.post(f"/application/{pk}/status", data={"status": "Submitted"})
    assert "Dash Person" in text_of(admin.get("/admin"))                  # assignment queue
    assert "Dash Person" not in text_of(reviewer.get("/reviewer"))
    admin.post(f"/application/{pk}/assign", data={"reviewer_id": uid("reviewer1")})
    assert "Dash Person" in text_of(reviewer.get("/reviewer"))
    reviewer.post(f"/application/{pk}/status", data={"status": "In Review"})
    reviewer.post(f"/application/{pk}/status", data={"status": "Correction Required", "reason": "Fix address"})
    page = text_of(staff.get("/staff"))
    assert "Correction zaroori" in page and "Fix address" in page


def test_admin_settings_and_audit(admin, appm):
    admin.post("/admin/settings/purpose", data={"name": "Rent"})
    admin.post("/admin/settings/fee", data={"name": "Loan 60,000", "loan_amount": "60000", "fee_amount": "1900"})
    with appm.app.app_context():
        assert appm.PurposeCategory.query.filter_by(name="Rent").first()
        f = appm.FeeCategory.query.filter_by(name="Loan 60,000").first()
        assert int(f.fee_amount) == 1900
        fid = f.id
    admin.post(f"/admin/settings/fee/{fid}", data={"name": "Loan 60,000", "loan_amount": "60000",
                                                   "fee_amount": "2000", "active": "1"})
    page = text_of(admin.get("/admin/audit", query_string={"action": "settings.fee_changed"}))
    assert "1900" in page and "2000" in page


def test_admin_only_powers_not_assignable(admin, staff, appm):
    admin.post("/admin/permissions", data={"Staff": sorted(appm.DEFAULT_ROLE_PERMS["Staff"] | {"audit.view", "reports.view"}),
                                           "Reviewer": sorted(appm.DEFAULT_ROLE_PERMS["Reviewer"])})
    with appm.app.app_context():
        perms = {p for (p,) in appm.db.session.query(appm.RolePermission.permission).filter_by(role="Staff")}
    assert "audit.view" not in perms and "reports.view" not in perms
    assert staff.get("/admin/audit").status_code == 403
