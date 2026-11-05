"""Build a throw-away load-test database: production schema + migrations + synthetic data."""
import glob, json, os, random, sys
from datetime import datetime, timedelta
import psycopg
from werkzeug.security import generate_password_hash

ROOT = "/home/user/Loan-department-"
PG = os.getenv("LOAD_PG", "host=/var/tmp/pgt port=54329 user=postgres")   # admin connection to a TEST server
DB = sys.argv[1] if len(sys.argv) > 1 else "tooba_load"
N_APPS = int(os.getenv("N_APPS", "50000"))
random.seed(7)

with psycopg.connect(PG + " dbname=postgres", autocommit=True) as c:
    c.execute(f"drop database if exists {DB} with (force)")
    c.execute(f"create database {DB}")
c = psycopg.connect(PG + f" dbname={DB}", autocommit=True)
c.execute(open(f"{ROOT}/tests/sql/prod_baseline.sql").read())
c.execute('insert into "user"(username, password_hash, role, active) values (%s,%s,%s,true)',
          ("admin", generate_password_hash("LoadAdmin1"), "Admin"))
skip = set(os.getenv("SKIP_MIGRATIONS", "").split(","))
for f in sorted(glob.glob(f"{ROOT}/supabase/migrations/*.sql")):
    if os.path.basename(f) in skip:
        print("skip", f); continue
    c.execute(open(f).read())
c.execute("select set_config('app.role','system',false)")
pw = generate_password_hash("LoadPass1")
users = []
for i in range(1, 21):
    users.append((f"reviewer{i}", "Reviewer"))
for i in range(1, 80):
    users.append((f"staff{i}", "Staff"))
with c.cursor() as cur:
    cur.executemany('insert into "user"(username, password_hash, role, active, full_name) values (%s,%s,%s,true,%s)',
                    [(u, pw, r, u.title()) for u, r in users])
ids = {r: [x for (x,) in c.execute('select id from "user" where role=%s', (r,)).fetchall()]
       for r in ("Staff", "Reviewer", "Admin")}
STAT = (["Draft"] * 6 + ["Submitted"] * 6 + ["Pending Review"] * 6 + ["In Review"] * 8 + ["Correction Required"] * 4
        + ["Submitted Again"] * 2 + ["Approved"] * 10 + ["Ready for Disbursement"] * 6 + ["Rejected"] * 8
        + ["Added to Disbursement Sheet"] * 3 + ["Disbursed"] * 10 + ["Completed"] * 10)
first = ["Muhammad", "Ahmed", "Ali", "Fatima", "Ayesha", "Bilal", "Usman", "Zainab", "Hamza", "Sana", "Qasim", "Imran"]
last = ["Khan", "Siddiqui", "Qureshi", "Sheikh", "Ansari", "Butt", "Malik", "Raza", "Hussain", "Iqbal"]
purposes = ["Business", "Education", "Liberation", "Marriage", "Medical", "Construction", "Other"]
fees = [(50000, 1700), (40000, 1500), (30000, 1300), (25000, 1200), (20000, 1100), (15000, 1000), (10000, 900)]
start = datetime(2025, 1, 1)
rows = []
for n in range(1, N_APPS + 1):
    st = random.choice(STAT)
    created = start + timedelta(minutes=random.randint(0, 60 * 24 * 670))
    name = f"{random.choice(first)} {random.choice(last)} {n}"
    cnic = f"{random.randint(10000, 99999)}{random.randint(1000000, 9999999)}{random.randint(1, 9)}"
    loan, fee = random.choice(fees)
    form = {"name": name, "cnic": cnic, "father": random.choice(first) + " " + random.choice(last),
            "phone": f"03{random.randint(100000000, 999999999)}", "address": "House %d, Orangi Town, Karachi" % n,
            "amount": f"{loan:,}", "purpose": random.choice(purposes), "income": "25,000",
            "notes": "x" * 600}
    rev = random.choice(ids["Reviewer"]) if st not in ("Draft",) else None
    approved = st in ("Approved", "Ready for Disbursement", "Added to Disbursement Sheet", "Disbursed", "Completed")
    rows.append((f"TBF-{created.year}-{n:05d}", name, cnic, st, random.choice(ids["Staff"]), json.dumps(form),
                 created, created + timedelta(days=random.randint(0, 20)),
                 str(n) if approved else None, rev, loan, random.choice(purposes), fee,
                 rev if approved else None, created + timedelta(days=5) if approved else None,
                 "Not eligible" if st == "Rejected" else None,
                 created + timedelta(days=1) if st != "Draft" else None,
                 created if random.random() > 0.01 else None))
with c.cursor() as cur:
    cur.executemany("""insert into application(app_id, applicant_name, cnic, status, created_by, form_json, created_at,
        updated_at, loan_number, reviewer_id, loan_amount, purpose_label, fee_amount, approved_by, approved_at,
        rejection_reason, submitted_at, deleted_at) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
        case when %s::timestamp is null then now() else null end)""", rows)
print("applications", c.execute("select count(*) from application").fetchone()[0])
# audit trail: ~5 rows per application
c.execute("""insert into audit_log(actor_id, actor_name, action, application_id, new_value, created_at)
  select a.created_by, 'staff', x.action, a.id,
         case when x.action = 'application.status' then jsonb_build_object('status', a.status) else null end,
         a.created_at + (x.n || ' hours')::interval
  from application a cross join (values (1,'application.created'),(2,'application.saved'),(3,'application.status'),
       (4,'document.uploaded'),(5,'application.status')) x(n, action)""")
c.execute("""insert into log(user_id, action, created_at)
  select (random()*99)::int + 1, case when random() < 0.1 then 'LOGIN_FAIL user=x ip=1.2.3.' || (random()*255)::int
         else 'Login section=staff ip=1.2.3.4' end, now() - (random()*400 || ' days')::interval
  from generate_series(1, 60000)""")
# disbursement sheets: 5 per month for 2026, entries from disbursed/added apps
c.execute("""insert into disbursement_batch(number, disb_date, month, year, status, created_by)
  select g, make_date(2026, ((g-1)/5)%12 + 1, ((g-1)%5)*5 + 1), ((g-1)/5)%12 + 1, 2026,
         'open', 1 from generate_series(1, 60) g""")
c.execute("""insert into disbursement_entry(batch_id, application_id, name, cnic, phone, amount, purpose, loan_number,
       fee_amount, added_by, disbursed_at)
  select ((row_number() over () - 1) % 60) + 1, a.id, a.applicant_name, a.cnic, '03001234567', a.loan_amount,
         a.purpose_label, a.loan_number, a.fee_amount, 1,
         case when a.status in ('Disbursed','Completed') then now() end
  from application a where a.status in ('Added to Disbursement Sheet','Disbursed','Completed')
   and a.deleted_at is null limit 2400""")
c.execute("""insert into cheque(entry_id, cheque_no, status, created_by)
  select id, 'CHQ' || id, 'Issued', 1 from disbursement_entry where id % 4 <> 0""")
c.execute("""insert into document(application_id, original_name, stored_name, kind, content_type, size, storage, data,
        uploaded_by, created_at)
  select e.application_id, 'p.jpg', 'load/' || e.application_id || '.jpg', 'photo', 'image/jpeg', 3, 'db',
         '\\xffd8ff'::bytea, 1, now() from disbursement_entry e""")
c.execute("insert into review_note(application_id, author_id, kind, body) select id, reviewer_id, 'correction', 'Fix CNIC' from application where status='Correction Required'")
# close the older sheets (as an Admin would)
c.execute("update disbursement_batch set status='closed', closed_by=1, closed_at=now() where number < 50")
c.execute("analyze")
for t in ("application", "audit_log", "log", "disbursement_entry", "cheque", "document"):
    print(t, c.execute(f"select count(*) from {t}").fetchone()[0])
