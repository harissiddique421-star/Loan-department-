-- =====================================================================================
-- Performance: search indexes, list/sort indexes, and faster (same) RLS policies.
--
-- SAFE TO RE-RUN. Additive only: no table, column or row is dropped or changed.
--   * indexes are created IF NOT EXISTS (they only speed up reads);
--   * the RLS policies are recreated with EXACTLY the same rules — the permission
--     functions are only wrapped as (select public.app_can(...)) so PostgreSQL evaluates
--     them once per query instead of once per row (Supabase's documented RLS performance
--     pattern). Who can see / change which row does not change.
-- Requires the earlier migrations (20261104…, 20261105…, 20261106…) — already applied.
--
-- ROLLBACK (keeps data): supabase/rollback/20261107000000_performance_indexes_rollback.sql
--   restores the previous policies exactly (copied from production before this change)
--   and drops only the new indexes / functions.
-- =====================================================================================
begin;

-- ------------------------------------------------------------------ search (contains / ILIKE)
-- The list pages search with ILIKE '%text%' on name, Application ID, Loan Number and CNIC.
-- A normal b-tree index cannot help a leading-% search; trigram GIN indexes can.
create schema if not exists extensions;
create extension if not exists pg_trgm with schema extensions;

create index if not exists application_name_trgm_idx
  on public.application using gin (applicant_name extensions.gin_trgm_ops);
create index if not exists application_cnic_trgm_idx
  on public.application using gin (cnic extensions.gin_trgm_ops);
create index if not exists application_app_id_trgm_idx
  on public.application using gin (app_id extensions.gin_trgm_ops);
create index if not exists application_loan_number_trgm_idx
  on public.application using gin (loan_number extensions.gin_trgm_ops);

-- ------------------------------------------------------------------ lists / dashboards
-- "Newest first" lists and dashboards (ORDER BY updated_at DESC), and max(updated_at)
-- for the cache stamp below.
create index if not exists application_updated_idx on public.application (updated_at);
-- status counts and status filters on live rows
create index if not exists application_live_status_idx
  on public.application (status) where deleted_at is null;
-- reviewer dashboard / reviewer list: my live applications by status
create index if not exists application_reviewer_status_idx
  on public.application (reviewer_id, status) where deleted_at is null;
-- disbursement: approved applications waiting for a sheet, oldest approval first
create index if not exists application_ready_idx
  on public.application (approved_at)
  where deleted_at is null and status in ('Approved', 'Ready for Disbursement');

-- ------------------------------------------------------------------ logs
-- login lock check: recent failed logins (created_at window)
create index if not exists log_created_idx on public.log (created_at);
-- reports (status history) and the audit page "action" filter
create index if not exists audit_log_action_idx on public.audit_log (action);
-- live rows of a disbursement sheet
create index if not exists disbursement_entry_live_batch_idx
  on public.disbursement_entry (batch_id) where removed_at is null;

-- ------------------------------------------------------------------ RLS: same rules, evaluated once per query
drop policy if exists user_insert on public."user";
create policy user_insert on public."user" for insert to tooba_app
  with check ((select public.app_can('users.manage')));
drop policy if exists user_update on public."user";
create policy user_update on public."user" for update to tooba_app
  using ((select public.app_can('users.manage')) or id = (select public.app_uid()))
  with check ((select public.app_can('users.manage')) or id = (select public.app_uid()));

drop policy if exists app_select on public.application;
create policy app_select on public.application for select to tooba_app using (
  (deleted_at is null or (select public.app_can('app.restore'))) and (
    (select public.app_can('app.view_all'))
    or created_by = (select public.app_uid()) or assigned_to = (select public.app_uid())
    or reviewer_id = (select public.app_uid())));
drop policy if exists app_insert on public.application;
create policy app_insert on public.application for insert to tooba_app
  with check ((select public.app_can('app.create')));
drop policy if exists app_update on public.application;
create policy app_update on public.application for update to tooba_app using (
  (select public.app_can('app.edit_any'))
  or ((select public.app_can('app.edit_own'))
      and (created_by = (select public.app_uid()) or assigned_to = (select public.app_uid()))
      and status in ('Draft', 'Correction Required'))
  or ((select public.app_can('app.review')) and reviewer_id = (select public.app_uid())))
with check (
  (select public.app_can('app.edit_any'))
  or ((select public.app_can('app.edit_own'))
      and (created_by = (select public.app_uid()) or assigned_to = (select public.app_uid())))
  or ((select public.app_can('app.review')) and reviewer_id = (select public.app_uid())));
drop policy if exists app_delete on public.application;
create policy app_delete on public.application for delete to tooba_app
  using ((select public.app_can('app.purge')));

drop policy if exists doc_insert on public.document;
create policy doc_insert on public.document for insert to tooba_app with check (
  (select public.app_can('doc.upload')) and exists (select 1 from public.application a where a.id = application_id));
drop policy if exists doc_update on public.document;
create policy doc_update on public.document for update to tooba_app using (
  (select public.app_can('doc.upload')) and exists (select 1 from public.application a where a.id = application_id));
drop policy if exists doc_delete on public.document;
create policy doc_delete on public.document for delete to tooba_app
  using ((select public.app_can('app.purge')));

drop policy if exists note_select on public.review_note;
create policy note_select on public.review_note for select to tooba_app using (
  exists (select 1 from public.application a where a.id = application_id)
  and (kind <> 'note' or (select public.app_can('app.review'))));
drop policy if exists note_insert on public.review_note;
create policy note_insert on public.review_note for insert to tooba_app with check (
  (select public.app_can('app.review')) and author_id = (select public.app_uid())
  and exists (select 1 from public.application a where a.id = application_id));

drop policy if exists audit_select on public.audit_log;
create policy audit_select on public.audit_log for select to tooba_app using (
  (select public.app_can('audit.view')) or actor_id = (select public.app_uid())
  or exists (select 1 from public.application a where a.id = application_id));
drop policy if exists audit_insert on public.audit_log;
create policy audit_insert on public.audit_log for insert to tooba_app with check (
  actor_id is not distinct from (select public.app_uid()) or (select public.app_role()) in ('system', 'legacy'));

drop policy if exists batch_select on public.disbursement_batch;
create policy batch_select on public.disbursement_batch for select to tooba_app using (
  (select public.app_can('disbursement.manage')) or (select public.app_can('cheque.manage')));
drop policy if exists entry_select on public.disbursement_entry;
create policy entry_select on public.disbursement_entry for select to tooba_app using (
  (select public.app_can('disbursement.manage')) or (select public.app_can('cheque.manage')));
drop policy if exists cheque_select on public.cheque;
create policy cheque_select on public.cheque for select to tooba_app using (
  (select public.app_can('disbursement.manage')) or (select public.app_can('cheque.manage')));

-- Search helper. Under RLS, PostgreSQL may not use an index for ILIKE (the operator is not
-- "leakproof"), so every search read all applications. This function finds the matching ids
-- with the trigram indexes and applies THE SAME visibility rule as the app_select policy;
-- the app then loads the rows through the normal query, where RLS checks every row again.
-- Matching is identical to before: ILIKE '%text%' on name, Application ID, Loan Number, CNIC
-- (and CNIC digits only, when the text had dashes).
create or replace function public.search_application_ids(q text, digits text default null)
  returns setof bigint language sql stable security definer set search_path = public as
$$ select a.id from public.application a
   where (a.applicant_name ilike '%' || q || '%' or a.app_id ilike '%' || q || '%'
          or a.loan_number ilike '%' || q || '%' or a.cnic ilike '%' || q || '%'
          or (digits is not null and a.cnic ilike '%' || digits || '%'))
     and (a.deleted_at is null or (select public.app_can('app.restore')))
     and ((select public.app_can('app.view_all')) or a.created_by = (select public.app_uid())
          or a.assigned_to = (select public.app_uid()) or a.reviewer_id = (select public.app_uid())) $$;
revoke all on function public.search_application_ids(text, text) from public;
grant execute on function public.search_application_ids(text, text) to tooba_app;

-- cheap "has anything changed?" stamp for the app's read cache. Returns only numbers
-- (largest audit id, newest application change, newest sheet change) — no row data.
create or replace function public.data_version() returns text
  language sql stable security definer set search_path = public as
$$ select concat_ws(':',
     (select max(id) from public.audit_log),
     (select extract(epoch from max(updated_at))::bigint from public.application),
     (select max(id) from public.disbursement_entry),
     (select max(id) from public.cheque),
     (select extract(epoch from max(coalesce(updated_at, created_at)))::bigint from public.cheque),
     (select count(*) from public.disbursement_entry where removed_at is null),
     (select max(id) from public."user")) $$;
revoke all on function public.data_version() from public;
grant execute on function public.data_version() to tooba_app;

analyze public.application;
analyze public.audit_log;
analyze public.log;

commit;
