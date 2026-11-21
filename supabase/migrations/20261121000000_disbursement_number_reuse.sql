-- =====================================================================================
-- A deleted disbursement sheet frees its number, so a new sheet can be created with the
-- same number (e.g. sheet 1 deleted by mistake -> create sheet 1 again).
-- The number stays unique among live (not deleted) sheets. Safe to re-run.
-- =====================================================================================
begin;
alter table public.disbursement_batch drop constraint if exists disbursement_batch_number_key;
create unique index if not exists disbursement_batch_number_live
  on public.disbursement_batch (number) where deleted_at is null;
commit;
