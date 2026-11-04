-- =====================================================================================
-- Admin can delete a disbursement sheet (soft delete: the sheet is hidden, its record and
-- audit history stay). Additive and safe to re-run.
-- =====================================================================================
begin;
alter table public.disbursement_batch
  add column if not exists deleted_at timestamp,
  add column if not exists deleted_by bigint references public."user"(id),
  add column if not exists delete_reason text;

-- A deleted sheet needs a reason, and only an open sheet can be deleted.
create or replace function public.tg_batch_guard() returns trigger
  language plpgsql set search_path = public as
$$ begin
  if old.deleted_at is not null then
    raise exception 'Disbursement sheet % is deleted', old.number using errcode = '42501';
  end if;
  if new.deleted_at is not null then
    if old.status <> 'open' or new.status <> 'open' then
      raise exception 'Only an open sheet can be deleted' using errcode = '42501';
    end if;
    if coalesce(btrim(new.delete_reason), '') = '' then
      raise exception 'A reason is required to delete a sheet' using errcode = '23514';
    end if;
    return new;
  end if;
  if old.status = 'closed' and new.status = 'closed' then
    raise exception 'Disbursement sheet % is closed', old.number using errcode = '42501';
  end if;
  if old.status <> new.status then
    if not public.app_can('period.close') then
      raise exception 'Not allowed to close or reopen' using errcode = '42501';
    end if;
    if new.status = 'open' and coalesce(btrim(new.reopen_reason), '') = '' then
      raise exception 'A reason is required to reopen' using errcode = '23514';
    end if;
  end if;
  return new;
end $$;
commit;
