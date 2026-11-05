# Load testing

**Never run this against the production site or database.** It logs in as synthetic users and
creates, edits, approves and rejects applications.

## 1. Test database (synthetic data)
Needs a local / staging PostgreSQL with a `tooba_app` role (as in `tests/sql/prod_baseline.sql`).

    N_APPS=50000 python loadtest/build_db.py tooba_load

Builds `tooba_load` from the production schema + every migration, then adds 100 users
(`admin` / `LoadAdmin1`, `reviewer1..20` and `staff1..79` / `LoadPass1`), 50,000 applications,
250,000 audit rows, 60,000 login-log rows, 60 disbursement sheets with cheques and photos.
Set `LOAD_PG` (libpq string of an admin connection to the *test* server) if it is not the default.

## 2. Run the app against it

    DATABASE_URL="postgresql://tooba_app:<password>@/tooba_load?host=/var/tmp/pgt&port=54329" \
    SECRET_KEY=<any long string> SECURE_COOKIES=0 PERF_TIMING=1 \
    gunicorn -w 4 -b 127.0.0.1:8000 app:app

`PERF_TIMING=1` adds a `Server-Timing` header (database time and query count) that the
load test records as "DB" rows.

## 3. Run Locust, increasing the load step by step

    pip install locust
    for u in 10 30 60 100 200; do
      LOAD_DB="host=/var/tmp/pgt port=54329 user=postgres dbname=tooba_load" \
      locust -f loadtest/locustfile.py --host http://127.0.0.1:8000 --users $u --spawn-rate 10 \
             --run-time 75s --headless --csv results/u$u --only-summary
    done

User mix: Admin 1 : Reviewer 3 : Staff 6, each waiting 1–3 s between clicks. Results per page
(count, failures, median, p95, p99) are in `results/u*_stats.csv`; "DB" rows are the server's
own database time. Rebuild the database between runs that should be compared (the test
approves / rejects applications).

Results of the first run: see `PERFORMANCE.md`.
