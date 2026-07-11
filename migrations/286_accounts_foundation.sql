-- 286_accounts_foundation.sql
-- Phase 1, increment 1 — the multi-tenant IDENTITY foundation.
--
-- Adds accounts / account_members / admins, a fixed SYSTEM account, the
-- current_account_ids() RLS helper, and the on-signup handler that gives every
-- new auth user a personal account. This is the base every per-account RLS
-- policy references (docs/design/phase-1-multitenancy-foundations.md §1).
--
-- NOT YET APPLIED. Requires Supabase Auth to be enabled (auth.users exists) and
-- must be applied via the Supabase MCP with the runbook. Migration NUMBER is
-- provisional — confirm the next free number at apply time (parallel branches
-- carry in-flight migrations; renumber if 286 is taken).
--
-- Deny-by-default posture is explicit here because Phase 0's default-ACL fix may
-- not be applied yet: every table enables RLS AND revokes anon/authenticated DML,
-- so these tables are never anon-writable regardless of the platform default ACL.

begin;

-- ── accounts: the tenant (billing unit) ────────────────────────────────────
create table if not exists accounts (
  id                 uuid primary key default gen_random_uuid(),
  kind               text not null default 'personal'
                       check (kind in ('personal', 'team', 'system')),
  name               text,
  stripe_customer_id text unique,
  created_at         timestamptz not null default now()
);

-- The one fixed SYSTEM account owns all platform/system-written rows, so
-- account_id can be NOT NULL everywhere (no NULL-owner ambiguity).
insert into accounts (id, kind, name)
  values ('00000000-0000-0000-0000-000000000000', 'system', 'system')
  on conflict (id) do nothing;

-- ── account_members: who belongs to which account ──────────────────────────
create table if not exists account_members (
  account_id uuid    not null references accounts(id) on delete cascade,
  user_id    uuid    not null references auth.users(id) on delete cascade,
  role       text    not null default 'owner'
                       check (role in ('owner', 'admin', 'member')),
  created_at timestamptz not null default now(),
  primary key (account_id, user_id)
);
create index if not exists account_members_user_idx on account_members (user_id);

-- ── admins: platform-admin allowlist (source of truth for the is_admin claim) ─
create table if not exists admins (
  user_id    uuid primary key references auth.users(id) on delete cascade,
  created_at timestamptz not null default now()
);

-- ── current_account_ids(): the ONE place tenancy is defined ─────────────────
-- Every per-account RLS policy references this. Today it returns the caller's
-- membership set (one personal account); teams/seats need no policy change.
create or replace function current_account_ids()
  returns setof uuid
  language sql
  stable
  security definer
  set search_path = public
as $$
  select am.account_id
  from account_members am
  where am.user_id = nullif(
    current_setting('request.jwt.claims', true)::jsonb ->> 'sub', ''
  )::uuid
$$;

-- is_platform_admin(): mirror helper for admin-gated policies/routes.
create or replace function is_platform_admin()
  returns boolean
  language sql
  stable
  security definer
  set search_path = public
as $$
  select exists (
    select 1 from admins a
    where a.user_id = nullif(
      current_setting('request.jwt.claims', true)::jsonb ->> 'sub', ''
    )::uuid
  )
$$;

-- ── on-signup handler: every new auth user gets a personal account ──────────
create or replace function handle_new_user()
  returns trigger
  language plpgsql
  security definer
  set search_path = public
as $$
declare
  new_account_id uuid;
begin
  insert into accounts (kind, name)
    values ('personal', coalesce(new.email, new.id::text))
    returning id into new_account_id;
  insert into account_members (account_id, user_id, role)
    values (new_account_id, new.id, 'owner');
  -- Wave 2 hooks seed_default_pipeline(new_account_id) here in the same tx.
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function handle_new_user();

-- ── RLS: deny-by-default, then scope reads to the caller ────────────────────
alter table accounts        enable row level security;
alter table account_members enable row level security;
alter table admins          enable row level security;

-- Never anon/authenticated-writable directly; all writes go through the
-- service-role API or the SECURITY DEFINER handler above.
revoke all on accounts, account_members, admins from anon, authenticated;
grant select on accounts, account_members to authenticated;

-- A member reads their own account(s) and the membership rows of those accounts.
create policy accounts_read_own on accounts
  for select to authenticated
  using (id in (select current_account_ids()));

create policy account_members_read_own on account_members
  for select to authenticated
  using (account_id in (select current_account_ids()));

-- admins is service-role/self only (no tenant read).
create policy admins_read_self on admins
  for select to authenticated
  using (user_id = nullif(current_setting('request.jwt.claims', true)::jsonb ->> 'sub', '')::uuid);

commit;
