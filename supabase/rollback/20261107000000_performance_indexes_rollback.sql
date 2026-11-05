-- Rollback for 20261107000000_performance_indexes.sql — run ONLY if that migration must be undone.
-- Restores the RLS policies exactly as they were in production on 2026-11-05 (copied from
-- pg_policies before the change) and removes the new indexes and helper functions.
-- No table, column or row is touched. Re-deploying the previous app version is not needed:
-- the app detects the missing functions at start-up and falls back to the old search.
begin;
drop policy if exists user_insert on public."user";
create policy user_insert on public."user" as permissive for insert to tooba_app with check (app_can('users.manage'::text));
drop policy if exists user_update on public."user";
create policy user_update on public."user" as permissive for update to tooba_app using ((app_can('users.manage'::text) or (id = app_uid()))) with check ((app_can('users.manage'::text) or (id = app_uid())));
drop policy if exists app_select on public.application;
create policy app_select on public.application as permissive for select to tooba_app using ((((deleted_at is null) or app_can('app.restore'::text)) and (app_can('app.view_all'::text) or (created_by = app_uid()) or (assigned_to = app_uid()) or (reviewer_id = app_uid()))));
drop policy if exists app_insert on public.application;
create policy app_insert on public.application as permissive for insert to tooba_app with check (app_can('app.create'::text));
drop policy if exists app_update on public.application;
create policy app_update on public.application as permissive for update to tooba_app using ((app_can('app.edit_any'::text) or (app_can('app.edit_own'::text) and ((created_by = app_uid()) or (assigned_to = app_uid())) and ((status)::text = any ((array['Draft'::character varying, 'Correction Required'::character varying])::text[]))) or (app_can('app.review'::text) and (reviewer_id = app_uid())))) with check ((app_can('app.edit_any'::text) or (app_can('app.edit_own'::text) and ((created_by = app_uid()) or (assigned_to = app_uid()))) or (app_can('app.review'::text) and (reviewer_id = app_uid()))));
drop policy if exists app_delete on public.application;
create policy app_delete on public.application as permissive for delete to tooba_app using (app_can('app.purge'::text));
drop policy if exists doc_insert on public.document;
create policy doc_insert on public.document as permissive for insert to tooba_app with check ((app_can('doc.upload'::text) and (exists (select 1 from application a where (a.id = document.application_id)))));
drop policy if exists doc_update on public.document;
create policy doc_update on public.document as permissive for update to tooba_app using ((app_can('doc.upload'::text) and (exists (select 1 from application a where (a.id = document.application_id)))));
drop policy if exists doc_delete on public.document;
create policy doc_delete on public.document as permissive for delete to tooba_app using (app_can('app.purge'::text));
drop policy if exists note_select on public.review_note;
create policy note_select on public.review_note as permissive for select to tooba_app using (((exists (select 1 from application a where (a.id = review_note.application_id))) and (((kind)::text <> 'note'::text) or app_can('app.review'::text))));
drop policy if exists note_insert on public.review_note;
create policy note_insert on public.review_note as permissive for insert to tooba_app with check ((app_can('app.review'::text) and (author_id = app_uid()) and (exists (select 1 from application a where (a.id = review_note.application_id)))));
drop policy if exists audit_select on public.audit_log;
create policy audit_select on public.audit_log as permissive for select to tooba_app using ((app_can('audit.view'::text) or (actor_id = app_uid()) or (exists (select 1 from application a where (a.id = audit_log.application_id)))));
drop policy if exists audit_insert on public.audit_log;
create policy audit_insert on public.audit_log as permissive for insert to tooba_app with check (((not (actor_id is distinct from app_uid())) or (app_role() = any (array['system'::text, 'legacy'::text]))));
drop policy if exists batch_select on public.disbursement_batch;
create policy batch_select on public.disbursement_batch as permissive for select to tooba_app using ((app_can('disbursement.manage'::text) or app_can('cheque.manage'::text)));
drop policy if exists entry_select on public.disbursement_entry;
create policy entry_select on public.disbursement_entry as permissive for select to tooba_app using ((app_can('disbursement.manage'::text) or app_can('cheque.manage'::text)));
drop policy if exists cheque_select on public.cheque;
create policy cheque_select on public.cheque as permissive for select to tooba_app using ((app_can('disbursement.manage'::text) or app_can('cheque.manage'::text)));

drop function if exists public.search_application_ids(text, text);
drop function if exists public.data_version();
drop index if exists public.application_name_trgm_idx;
drop index if exists public.application_cnic_trgm_idx;
drop index if exists public.application_app_id_trgm_idx;
drop index if exists public.application_loan_number_trgm_idx;
drop index if exists public.application_updated_idx;
drop index if exists public.application_live_status_idx;
drop index if exists public.application_reviewer_status_idx;
drop index if exists public.application_ready_idx;
drop index if exists public.log_created_idx;
drop index if exists public.audit_log_action_idx;
drop index if exists public.disbursement_entry_live_batch_idx;
-- pg_trgm is left installed (harmless; other objects may use it).
commit;
