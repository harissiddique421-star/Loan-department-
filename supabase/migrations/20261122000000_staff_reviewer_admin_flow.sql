-- =====================================================================================
-- Staff -> Reviewer -> Admin hand-off.
--   * Staff (permission app.assign_reviewer) may give their own submitted application to a
--     Reviewer. RLS does not let Staff update a submitted application, so this goes through
--     the security-definer function staff_assign_reviewer(), which checks everything itself.
--   * A Reviewer (permission app.forward) may, after looking at an assigned application,
--     forward it to Admin (forwarded_to / forwarded_by / forwarded_at). Admin then decides.
-- Additive only, safe to re-run.
-- =====================================================================================
begin;

alter table public.application
  add column if not exists forwarded_to bigint references public."user"(id),
  add column if not exists forwarded_by bigint references public."user"(id),
  add column if not exists forwarded_at timestamp;
create index if not exists application_forwarded_idx
  on public.application (forwarded_at) where forwarded_at is not null and deleted_at is null;

insert into public.role_permission (role, permission) values
  ('Staff', 'app.assign_reviewer'), ('Reviewer', 'app.forward')
on conflict do nothing;

-- Only someone with app.forward may mark an application as forwarded, and only in their own
-- name. Clearing the mark (it is cleared when the status changes) is allowed to anyone who may
-- update the row at all.
create or replace function public.tg_forward_guard() returns trigger
  language plpgsql set search_path = public as
$$ begin
  if public.app_role() in ('Admin', 'system', 'legacy') then
    return new;
  end if;
  if (new.forwarded_to, new.forwarded_by, new.forwarded_at)
       is distinct from (old.forwarded_to, old.forwarded_by, old.forwarded_at)
     and new.forwarded_at is not null then
    if not public.app_can('app.forward') or new.forwarded_by is distinct from public.app_uid() then
      raise exception 'Not allowed to forward this application' using errcode = '42501';
    end if;
  end if;
  return new;
end $$;
drop trigger if exists forward_guard on public.application;
create trigger forward_guard before update of forwarded_to, forwarded_by, forwarded_at on public.application
  for each row execute function public.tg_forward_guard();

-- Staff gives one of their own submitted applications to an active Reviewer.
-- Returns the new status. Every rule is checked here, so calling it directly is safe.
create or replace function public.staff_assign_reviewer(p_app bigint, p_reviewer bigint)
  returns varchar language plpgsql security definer set search_path = public as
$$ declare
  a public.application%rowtype;
  uid bigint := public.app_uid();
  role_before text := current_setting('app.role', true);
  new_status varchar;
begin
  if uid is null or not public.app_can('app.assign_reviewer') then
    raise exception 'Not allowed to assign a reviewer' using errcode = '42501';
  end if;
  select * into a from public.application where id = p_app for update;
  if not found or a.deleted_at is not null
     or not (a.created_by = uid or a.assigned_to is not distinct from uid) then
    raise exception 'Application not found' using errcode = '42501';
  end if;
  if a.status not in ('Submitted', 'Submitted Again', 'Pending Review') then
    raise exception 'Reviewer can only be chosen before the review starts' using errcode = '42501';
  end if;
  if not exists (select 1 from public."user" u where u.id = p_reviewer and u.role = 'Reviewer'
                 and u.active and u.deleted_at is null) then
    raise exception 'Not an active reviewer' using errcode = '23514';
  end if;
  new_status := case when a.status in ('Submitted', 'Submitted Again') then 'Pending Review' else a.status end;
  -- the status guard trigger would refuse Submitted -> Pending Review for Staff (it needs
  -- app.assign); every rule for this hand-off has been checked above, so act as the system
  -- for this one update and then switch back.
  perform set_config('app.role', 'system', true);
  update public.application set reviewer_id = p_reviewer, status = new_status,
         updated_by = uid, updated_at = now() at time zone 'utc'
   where id = p_app;
  perform set_config('app.role', coalesce(role_before, ''), true);
  return new_status;
end $$;
revoke all on function public.staff_assign_reviewer(bigint, bigint) from public, anon, authenticated;
grant execute on function public.staff_assign_reviewer(bigint, bigint) to tooba_app;

commit;
