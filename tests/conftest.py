"""Test setup.

PostgreSQL mode (recommended — tests RLS, triggers and the migration itself):
    TEST_PG="host=/var/tmp/pgt port=54329 user=postgres" pytest tests
A fresh database is built from tests/sql/prod_baseline.sql (the current production schema)
plus legacy rows, then every file in supabase/migrations is applied. The app then connects
as the restricted `tooba_app` role, exactly like production.

Without TEST_PG the same HTTP tests run against a temporary SQLite file.
"""
import os, sys, glob, tempfile
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PG = os.getenv("TEST_PG")
DBNAME = "tooba_test"
ADMIN_PW = "AdminPass123!"


def migration_files():
    return sorted(glob.glob(os.path.join(ROOT, "supabase", "migrations", "*.sql")))


def pg_admin(dbname="postgres"):
    import psycopg
    return psycopg.connect(PG + f" dbname={dbname}", autocommit=True)


def build_pg():
    from werkzeug.security import generate_password_hash
    with pg_admin() as c:
        c.execute(f"drop database if exists {DBNAME} with (force)")
        c.execute(f"create database {DBNAME}")
    with pg_admin(DBNAME) as c:
        c.execute(open(os.path.join(ROOT, "tests", "sql", "prod_baseline.sql")).read())
        # legacy rows, shaped like the live data
        c.execute('insert into "user"(username, password_hash, role, active) values (%s, %s, %s, true)',
                  ("admin", generate_password_hash(ADMIN_PW), "Admin"))
        c.execute("""insert into application(app_id, applicant_name, cnic, status, form_json, created_by,
                     created_at, updated_at) values ('TBF-0001', 'Legacy Person', '1111122222333', 'Pending',
                     '{"name": "Legacy Person", "cnic": "1111122222333", "amount": "50,000"}', 1, now(), now())""")
        c.execute("""insert into document(application_id, original_name, stored_name, kind, content_type, size,
                     storage, data, uploaded_by, created_at)
                     values (1, 'p.jpg', 'TBF-0001/legacy.jpg', 'photo', 'image/jpeg', 3, 'db', '\\x010203', 1, now())""")
        c.execute("insert into log(user_id, action, application_id, created_at) values (1, 'Created TBF-0001', 1, now())")
        for f in migration_files():
            c.execute(open(f).read())


def pg_app_url():
    parts = dict(kv.split("=", 1) for kv in PG.split())
    return f"postgresql://tooba_app:test-only@/{DBNAME}?host={parts['host']}&port={parts['port']}"


if PG:
    build_pg()
    os.environ["DATABASE_URL"] = pg_app_url()
    os.environ["SECURE_COOKIES"] = "0"
else:
    _tmp = tempfile.mkdtemp()
    os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
    os.environ["ADMIN_PASSWORD"] = ADMIN_PW
os.environ.pop("SUPABASE_URL", None)

import app as appmod  # noqa: E402

appmod.app.config.update(WTF_CSRF_ENABLED=False, TESTING=True)


@pytest.fixture(scope="session")
def appm():
    return appmod


@pytest.fixture
def pg():
    if not PG:
        pytest.skip("PostgreSQL-only test (set TEST_PG)")
    with pg_admin(DBNAME) as c:
        yield c


class UserClient:
    def __init__(self, username, password, portal="staff"):
        self.c = appmod.app.test_client()
        self.username = username
        r = self.c.post(f"/login/{portal}", data={"username": username, "password": password})
        assert r.status_code == 302, (username, portal, r.status_code)

    def get(self, *a, **k):
        return self.c.get(*a, **k)

    def post(self, *a, **k):
        return self.c.post(*a, **k)


_users = {}


def make_user(role, name):
    if name not in _users:
        admin = UserClient("admin", ADMIN_PW, "admin")
        r = admin.post("/admin/users", data={"username": name, "password": "Password123", "role": role,
                                             "full_name": name.title()})
        assert r.status_code == 302
        _users[name] = True
    return UserClient(name, "Password123", role.lower())


@pytest.fixture
def admin():
    return UserClient("admin", ADMIN_PW, "admin")


@pytest.fixture
def staff():
    return make_user("Staff", "staff1")


@pytest.fixture
def staff2():
    return make_user("Staff", "staff2")


@pytest.fixture
def reviewer():
    return make_user("Reviewer", "reviewer1")


@pytest.fixture
def reviewer2():
    return make_user("Reviewer", "reviewer2")


def uid(name):
    with appmod.app.app_context():
        return appmod.User.query.filter_by(username=name).first().id


TINY_JPG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b080001000101011100ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbd3ffd9")
REQUIRED_CNIC = ["cnic_app_front", "cnic_app_back", "cnic_w1_front", "cnic_w1_back", "cnic_w2_front", "cnic_w2_back"]


def upload_kind(client, pk, kind, content=TINY_JPG):
    import io
    r = client.post(f"/application/{pk}/upload", data={"kind": kind, "file": (io.BytesIO(content), f"{kind}.jpg")},
                    content_type="multipart/form-data")
    assert r.status_code == 302, (kind, r.status_code)


def complete_app(client, pk, purpose="Business", fee=1700):
    """Everything needed to submit: the 6 mandatory CNIC images, purpose and form fee."""
    for k in REQUIRED_CNIC:
        upload_kind(client, pk, k)
    with appmod.app.app_context():
        p = appmod.PurposeCategory.query.filter_by(name=purpose).first().id
        f = appmod.FeeCategory.query.filter(appmod.FeeCategory.fee_amount == fee).first().id
    r = client.post(f"/application/{pk}/details", data={"purpose_id": p, "fee_category_id": f})
    assert r.status_code == 302


def new_app(client, complete=True, **fields):
    r = client.post("/application/new")
    assert r.status_code == 302, r.status_code
    app_pk = int(r.headers["Location"].rstrip("/").split("/")[-2])
    data = {"name": "Test Applicant", "cnic": "3520112345671", "amount": "50000"}
    data.update(fields)
    r = client.post(f"/application/{app_pk}/form", data=data)
    assert r.status_code == 302, r.status_code
    if complete:
        complete_app(client, app_pk)
    return app_pk


def get_row(pk):
    with appmod.app.app_context():
        a = appmod.db.session.get(appmod.Application, pk)
        appmod.db.session.expunge(a)
        return a
