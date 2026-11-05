"""Performance changes must not change behaviour: same search results, fresh counts, same access."""
import pytest
from conftest import new_app, appmod, uid, get_row


def text_of(r):
    return r.get_data(as_text=True)


def test_search_function_matches_original_rls_search(pg, staff, staff2, admin):
    a1 = new_app(staff, name="Perf Search One", cnic="4210155500001")
    new_app(staff2, name="Perf Search Two", cnic="4210155500002")
    import psycopg, conftest
    for role, user in (("Admin", "admin"), ("Staff", "staff1"), ("Staff", "staff2")):
        u = uid(user)
        for q, digits in (("Perf Search", None), ("42101-55500", "4210155500"), ("TBF-", None)):
            with psycopg.connect(conftest.pg_app_url()) as c:
                c.execute("select set_config('app.role', %s, false), set_config('app.user_id', %s, false)",
                          (role, str(u)))
                like = f"%{q}%"
                old = {r[0] for r in c.execute(
                    "select id from application where applicant_name ilike %s or app_id ilike %s "
                    "or loan_number ilike %s or cnic ilike %s" + (" or cnic ilike %s" if digits else ""),
                    [like] * 4 + ([f"%{digits}%"] if digits else []))}
                new = {r[0] for r in c.execute(
                    "select id from application where id in (select public.search_application_ids(%s, %s))",
                    (q, digits))}
                assert old == new, (role, user, q)
    # staff2 cannot find staff1's application through the page either
    assert "Perf Search One" not in text_of(staff2.get("/staff/applications", query_string={"q": "Perf Search"}))
    assert "Perf Search One" in text_of(staff.get("/staff/applications", query_string={"q": "Perf Search"}))


def test_cached_counts_change_immediately(pg, admin, staff):
    if not appmod.PERF_DB:
        pytest.skip("performance migration not applied")
    before = text_of(admin.get("/admin"))
    pk = new_app(staff, name="Count Me", cnic="4210155500003")
    staff.post(f"/application/{pk}/loan-number", data={"loan_number": "PERF-1"})
    assert staff.post(f"/application/{pk}/status", data={"status": "Submitted"}).status_code == 302
    after = text_of(admin.get("/admin"))
    assert before != after                                   # new version, fresh numbers
    import re
    n = lambda page: int(re.search(r'<small>Submitted</small><b>(\d+)</b>', page).group(1))
    assert n(after) == n(before) + 1


def test_list_pagination(admin, staff, monkeypatch):
    for i in range(3):
        new_app(staff, name=f"Pager {i}", cnic=f"42101555001{i}0")
    monkeypatch.setattr(appmod, "LIST_PAGE_SIZE", 2)
    p1 = text_of(admin.get("/admin/applications", query_string={"q": "Pager"}))
    assert "Agla page" in p1 and "Pichla page" not in p1
    p2 = text_of(admin.get("/admin/applications", query_string={"q": "Pager", "page": 2}))
    assert "Pichla page" in p2 and "Agla page" not in p2
    names = lambda t: {f"Pager {i}" for i in range(3) if f"Pager {i}" in t}
    assert names(p1) | names(p2) == {"Pager 0", "Pager 1", "Pager 2"} and not names(p1) & names(p2)


def test_static_files_are_fingerprinted_and_cached():
    c = appmod.app.test_client()
    page = text_of(c.get("/login/admin"))
    import re
    url = re.search(r'href="(/static/style\.css\?v=[0-9a-f]{10})"', page).group(1)
    r = c.get(url)
    assert r.status_code == 200 and "max-age=31536000" in r.headers["Cache-Control"]
    assert "max-age=3600" in c.get("/static/style.css").headers["Cache-Control"]


def test_heavy_downloads_are_rate_limited(admin, monkeypatch):
    monkeypatch.setitem(appmod.app.config, "TESTING", False)
    appmod._rl_hits.clear()
    codes = [admin.get("/admin/export").status_code for _ in range(12)]
    assert codes[:10] == [200] * 10 and codes[10] == 429
    assert admin.get("/admin").status_code == 200          # normal pages still fine
    appmod._rl_hits.clear()


def test_staff_dashboard_shows_latest_correction_note(admin, staff, reviewer):
    pk = new_app(staff, name="Note Person", cnic="4210155500099")
    staff.post(f"/application/{pk}/loan-number", data={"loan_number": "PERF-2"})
    staff.post(f"/application/{pk}/status", data={"status": "Submitted"})
    admin.post(f"/application/{pk}/assign", data={"reviewer_id": uid("reviewer1")})
    reviewer.post(f"/application/{pk}/status", data={"status": "In Review"})
    reviewer.post(f"/application/{pk}/status", data={"status": "Correction Required", "reason": "Photo blurry"})
    assert get_row(pk).status == "Correction Required"
    assert "Photo blurry" in text_of(staff.get("/staff"))
