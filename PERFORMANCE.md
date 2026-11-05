# Performance report — Tooba Foundation Loan Department

All measurements were made on a **separate test database** with synthetic data (50,000
applications, 100 users, 250,000 audit rows, 60,000 login-log rows, 60 disbursement sheets,
2,400 sheet rows). No load test touched production. Same app code, Flask + gunicorn 4 workers,
connected as the restricted `tooba_app` role with Row Level Security on and one new database
connection per request (as on Vercel). Tool: Locust (`loadtest/`).

## 1. Tables inspected
`application`, `document`, `user`, `log`, `audit_log`, `review_note`, `role_permission`,
`status_transition`, `purpose_category`, `fee_category`, `app_id_counter`, `disbursement_batch`,
`disbursement_entry`, `cheque`, `gsheet_year`, `gsheet_sync`, `gsheet_sync_log` — with their RLS
policies (production snapshot kept in `supabase/rollback/…_rollback.sql`) and the Supabase
performance advisor.

## 2. Existing indexes found (production)
application: `id` (pk), `app_id` (unique **and** a duplicate plain index), `cnic`, `loan_number`
(+ unique partial), `status`, `reviewer_id`, `created_by`, `assigned_to`, `lower(applicant_name)`;
audit_log: `application_id`, `created_at`, `actor_id`; document: `application_id`,
`(application_id, kind)`, `stored_name`; review_note: `application_id`; log: `application_id`;
disbursement_batch: `number`, `(year, month)`; disbursement_entry: `batch_id`,
unique `application_id where removed_at is null`; cheque: `entry_id`, unique active `cheque_no`,
unique active entry; settings tables: primary keys / names.

None of them could serve the app's searches (`ILIKE '%text%'`), and RLS called a permission
function for every row of every query.

## 3. New indexes (migration `supabase/migrations/20261107000000_performance_indexes.sql`)
| Index | Serves |
|---|---|
| `application_{name,cnic,app_id,loan_number}_trgm_idx` (GIN trigram) | name / CNIC / Application ID / Loan Number search |
| `application_updated_idx (updated_at)` | "newest first" lists and dashboards |
| `application_live_status_idx (status) where deleted_at is null` | status counts and filters |
| `application_reviewer_status_idx (reviewer_id, status) where deleted_at is null` | reviewer dashboard / list |
| `application_ready_idx (approved_at) where … Approved / Ready for Disbursement` | applications waiting for a sheet |
| `log_created_idx (created_at)` | login-lock check on every login |
| `audit_log_action_idx (action)` | reports (status history), audit filter |
| `disbursement_entry_live_batch_idx (batch_id) where removed_at is null` | sheet rows and totals |

Not added on purpose: indexes for the 21 "unindexed foreign keys" the advisor lists — they point
to `user` (who created / closed / removed …), which is never deleted, so those indexes would cost
writes and space without speeding up any page. The duplicate `application_app_id_idx` is left in
place (nothing is dropped); it can be removed later.

**RLS made faster, rules unchanged:** each policy now calls `(select public.app_can(...))` /
`(select public.app_uid())`, so PostgreSQL evaluates the permission once per query instead of
once per row (Supabase's recommended pattern). Tests prove each role sees exactly the same rows.

**Search helper `public.search_application_ids(q, digits)`:** with RLS on, PostgreSQL will not use
an index for ILIKE (the operator is not "leakproof"), so every search read the whole table. The
function finds matching ids through the trigram indexes and applies the same visibility rule as
the policy; the app then loads those rows through the normal query, where RLS checks each row
again. Matching is identical to before (verified per role in `tests/test_performance.py`).

**`public.data_version()`:** returns only a few numbers (newest audit id, newest change times)
used to know when cached results are outdated.

## 4. Queries optimised (same results)
* List pages (Admin / Reviewer / Staff): 13 COUNT queries → 1 GROUP BY; search through the index;
  real pagination (200 per page, Previous / Next links) instead of silently stopping at 200.
* Admin dashboard: 13 status counts + 3 user counts → 2 grouped queries.
* Reviewer and Staff dashboards: counts in one GROUP BY; Staff "correction notes" — one query
  instead of one per application.
* Disbursement list: sheet totals in one grouped query (was 2 queries per sheet); "ready" count
  with COUNT instead of loading every eligible application.
* Disbursement sheet: built once (was twice); cheques for all rows in one query (was one per row,
  twice).
* Reports: loads only the 8 columns it uses as light rows, counts months in one pass. Output was
  compared byte-for-byte with the old code on the 50,000-row data (2025 and 2026): identical.

## 5. Caching
* **Server side** (`cache.py`): dashboard counts, list counts, monthly report totals and the
  user-name list. Each entry is tied to `data_version()`, read once per request. Almost every
  change in the app writes an audit row, so any create / edit / approve / reject / delete /
  sheet change makes old entries unusable on every server instance immediately; a 5-minute TTL is
  a second safety net. Keys include role and user id wherever RLS decides what a user sees.
  Nothing sensitive is cached: no sessions, passwords, permission decisions or database objects.
* **Browser**: CSS / JS / images get `?v=<fingerprint>` and are cached for a year; a deploy
  changes the fingerprint so users get new files at once. Signed-in pages stay `no-store`.

## 6. Load-testing setup
`loadtest/build_db.py` builds the synthetic database; `loadtest/locustfile.py` runs Admin (1),
Reviewer (3) and Staff (6) users in that ratio: login, dashboards, lists, status filter, name /
CNIC / Application ID / Loan Number search, open application, new application, form save,
approve, reject, disbursement list and sheet, monthly report, audit log. The app's optional
`PERF_TIMING=1` adds a `Server-Timing` header so the database time per request is recorded too.
See `loadtest/README.md`.

## 7. Before and after (average response time, ms)
| Page | Before, 10 users | Before, 30 users | After, 30 users | After, 100 users |
|---|---|---|---|---|
| Search by CNIC | 2,505 | 4,203 | 51 | 61 |
| Search by Application ID | 2,888 | 4,700 | 55 | 64 |
| Search by Loan Number | — | 3,386 | 47 | 69 |
| Search by name | 380 | 1,977 | 52 | 59 |
| Admin dashboard | 1,708 | 3,039 | 53 | 62 |
| Admin applications list | 1,633 | 3,568 | 65 | 68 |
| Reviewer dashboard | 260 | 1,893 | 86 | 96 |
| Staff dashboard | 199 | 2,130 | 38 | 50 |
| Open application | 414 | 2,295 | 41 | 57 |
| Save form | 116 | 1,725 | 27 | 38 |
| Approve / reject | 84 / 398 | 3,796 / 1,319 | 66 / 67 | 86 / 98 |
| Disbursement list / sheet | 642 / 167 | 2,875 / 2,778 | 43 / 137 | 40 / 68 |
| Monthly report (first load) | 6,788 | 7,309 | 1,156 | 1,064 |
| Login (password check) | 445 | 2,581 | 397 | 427 |

| Users | Requests/s | Avg | Errors |
|---|---|---|---|
| Before: 10 | 3.9 | 739 ms | 0 |
| Before: 30 | 7.3 | 2,373 ms (queueing) | 0 |
| After: 30 | 15.8 | 60 ms | 0 |
| After: 60 | 31.8 | 61 ms | 0 |
| After: 100 | 50.7 | 71 ms | 0 |
| After: 200 | 78.6 | 592 ms | 0 |

Database time per request after the change: 10–40 ms for every page except the monthly report
(≈ 0.5 s on first load, then cached).

## 8. Remaining bottlenecks
* At 200 simultaneous users on 4 test workers the CPU is full — mostly the **password check at
  login** (scrypt, deliberately slow against password cracking; not weakened). The database stays
  at 10–50 ms. On Vercel each request runs in its own instance, so this scales out by itself.
* **Monthly report** still reads every live application once (≈ 1 s at 50,000) when the data has
  changed; afterwards it is cached. Moving its counting into SQL would make it ~100 ms.
* **Google Sheets** is updated inside the request after a sheet change (two tabs) — this adds
  Google's response time to that click. Running it only from the cron would make clicks faster
  but the Google copy would lag; not changed without your decision.
* Rate limits are per server instance (Vercel runs several); a global limit belongs in the
  Vercel Firewall.

## 9. Security
RLS stays enabled on every table with the same rules; the two new functions only return ids /
numbers and apply the same visibility rule; nothing is exposed to the browser; the service key is
not used; caches are keyed per role / user and invalidated by the database version; rate limits
(per signed-in user, or per address when signed out): 600 requests/min general, 120 searches/min,
10 heavy downloads/min (exports, backups, Excel), 300 photo requests/min for Google Sheets; double
submits are blocked in the browser for 4 s.

## 10. Recommended next steps
1. Apply `20261107000000_performance_indexes.sql` in production (rollback script included).
2. Add a Vercel Firewall rate-limit rule (e.g. 300 requests / minute per IP on `/login/*`).
3. When applications pass ~100,000: compute report totals in SQL, and consider a Supabase
   compute upgrade only if the database's CPU (Supabase → Reports) is consistently high.
4. Remove the duplicate index `application_app_id_idx` in a later clean-up.
5. Keep Supabase's daily backups plus the nightly Google Drive backup.
