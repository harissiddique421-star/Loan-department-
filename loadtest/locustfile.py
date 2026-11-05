"""Load test for the Tooba Foundation Loan Department app (Locust).

NEVER point this at the production site: it logs in as test users and creates, edits,
approves and rejects applications. Run it against a local / staging copy built with
loadtest/build_db.py (synthetic data). See loadtest/README.md.

    locust -f loadtest/locustfile.py --host http://127.0.0.1:8000 \
           --users 50 --spawn-rate 5 --run-time 2m --headless --csv results/run

Environment:
    LOAD_DB      libpq string of the *test* database (to pick application ids),
                 e.g. "host=/var/tmp/pgt port=54329 user=postgres dbname=tooba_load"
    LOAD_PASS    password of the synthetic staff / reviewer users (default LoadPass1)
    ADMIN_PASS   password of the synthetic admin user (default LoadAdmin1)
"""
import os
import random
import re

from locust import HttpUser, between, events, task

DB = os.getenv("LOAD_DB", "host=/var/tmp/pgt port=54329 user=postgres dbname=tooba_load")
PASS = os.getenv("LOAD_PASS", "LoadPass1")
ADMIN_PASS = os.getenv("ADMIN_PASS", "LoadAdmin1")
CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')
TIMING = re.compile(r'db;dur=([\d.]+);desc="(\d+) queries"')
POOL = {}


@events.init.add_listener
def load_ids(environment, **_):
    """Sample ids from the test database once (read only)."""
    import psycopg
    if "tooba_load" not in DB and os.getenv("I_KNOW_THIS_IS_NOT_PRODUCTION") != "1":
        raise SystemExit("LOAD_DB must point at the synthetic test database")
    with psycopg.connect(DB) as c:
        POOL["apps"] = [r for r in c.execute(
            "select id, app_id, cnic, loan_number, applicant_name from application where deleted_at is null "
            "order by random() limit 3000")]
        POOL["batches"] = [r[0] for r in c.execute("select id from disbursement_batch where deleted_at is null")]
        POOL["review"] = {}
        for rid, uname in c.execute("select id, username from \"user\" where role='Reviewer'"):
            POOL["review"][uname] = [r[0] for r in c.execute(
                "select id from application where reviewer_id=%s and status='In Review' and deleted_at is null "
                "order by random() limit 400", (rid,))]


@events.request.add_listener
def db_time(request_type, name, response_time, response_length, response=None, exception=None, **_):
    """Record the server's own DB time (Server-Timing, app started with PERF_TIMING=1)."""
    if response is None:
        return
    m = TIMING.search(response.headers.get("Server-Timing", ""))
    if m:
        events.request.fire(request_type="DB", name=name, response_time=float(m.group(1)),
                            response_length=int(m.group(2)), exception=None, context={})


class Base(HttpUser):
    abstract = True
    wait_time = between(1, 3)          # a person clicking, not a script hammering
    portal, username, password = "staff", "staff1", PASS

    def on_start(self):
        r = self.client.get(f"/login/{self.portal}", name="login page")
        token = CSRF.search(r.text).group(1)
        r = self.client.post(f"/login/{self.portal}", name=f"login {self.portal}",
                             data={"csrf_token": token, "username": self.username, "password": self.password})
        if r.status_code >= 400 or "/login" in r.url:
            raise RuntimeError(f"login failed for {self.username}: {r.status_code}")
        self.token = token

    def app_row(self):
        return random.choice(POOL["apps"])

    def search(self, kind):
        a = self.app_row()
        q = {"name": a[4].split()[0] + " " + a[4].split()[1], "cnic": a[2][:5] + "-" + a[2][5:12],
             "appid": a[1], "loan": a[3] or a[1]}[kind]
        self.client.get(self.list_url, params={"q": q}, name=f"search {kind}")


class AdminUser(Base):
    weight = 1
    portal, username, password = "admin", "admin", ADMIN_PASS
    list_url = "/admin/applications"

    @task(5)
    def dashboard(self):
        self.client.get("/admin", name="admin dashboard")

    @task(4)
    def applications(self):
        self.client.get("/admin/applications", name="admin applications list")

    @task(2)
    def by_status(self):
        self.client.get("/admin/applications", params={"status": "In Review"}, name="admin list by status")

    @task(3)
    def search_name(self):
        self.search("name")

    @task(2)
    def search_cnic(self):
        self.search("cnic")

    @task(2)
    def search_appid(self):
        self.search("appid")

    @task(2)
    def search_loan(self):
        self.search("loan")

    @task(3)
    def view(self):
        self.client.get(f"/application/{self.app_row()[0]}", name="view application")

    @task(2)
    def disbursement_list(self):
        self.client.get("/disbursement", name="disbursement list")

    @task(2)
    def sheet(self):
        self.client.get(f"/disbursement/{random.choice(POOL['batches'])}", name="disbursement sheet")

    @task(1)
    def reports(self):
        self.client.get("/admin/reports", params={"year": 2026}, name="reports (monthly totals)")

    @task(1)
    def audit(self):
        self.client.get("/admin/audit", name="audit log")


class ReviewerUser(Base):
    weight = 3
    portal = "reviewer"
    list_url = "/reviewer/applications"

    def on_start(self):
        self.username = f"reviewer{random.randint(1, 20)}"
        super().on_start()

    @task(5)
    def dashboard(self):
        self.client.get("/reviewer", name="reviewer dashboard")

    @task(3)
    def applications(self):
        self.client.get("/reviewer/applications", name="reviewer applications list")

    @task(2)
    def search_name(self):
        self.search("name")

    @task(1)
    def search_cnic(self):
        self.search("cnic")

    @task(2)
    def decide(self):
        ids = POOL["review"].get(self.username) or []
        if not ids:
            return
        pk = ids.pop()
        self.client.get(f"/application/{pk}", name="view application")
        if random.random() < 0.7:
            self.client.post(f"/application/{pk}/status", name="approve",
                             data={"csrf_token": self.token, "status": "Approved"})
        else:
            self.client.post(f"/application/{pk}/status", name="reject",
                             data={"csrf_token": self.token, "status": "Rejected", "reason": "Load test"})


class StaffUser(Base):
    weight = 6
    portal = "staff"
    list_url = "/staff/applications"

    def on_start(self):
        self.username = f"staff{random.randint(1, 79)}"
        super().on_start()
        self.mine = []

    @task(5)
    def dashboard(self):
        self.client.get("/staff", name="staff dashboard")

    @task(3)
    def applications(self):
        self.client.get("/staff/applications", name="staff applications list")

    @task(2)
    def search_all(self):
        a = self.app_row()
        self.client.get("/staff/applications", params={"scope": "all", "q": a[2]}, name="search cnic")

    @task(1)
    def search_appid(self):
        a = self.app_row()
        self.client.get("/staff/applications", params={"scope": "all", "q": a[1]}, name="search appid")

    @task(1)
    def new_application(self):
        r = self.client.post("/application/new", data={"csrf_token": self.token}, name="new application",
                             allow_redirects=False)
        m = re.search(r"/application/(\d+)/form", r.headers.get("Location", ""))
        if m:
            self.mine.append(int(m.group(1)))
            self.save(int(m.group(1)))

    @task(2)
    def edit_application(self):
        if self.mine:
            self.save(random.choice(self.mine))

    def save(self, pk):
        n = random.randint(1, 10 ** 6)
        self.client.post(f"/application/{pk}/form", name="save form",
                         headers={"X-Requested-With": "fetch"},
                         data={"csrf_token": self.token, "name": f"Load Test {n}",
                               "cnic": f"42101{n:07d}1", "address": "Orangi Town"})
