-- 298_billing_entitlements.sql
-- Phase 1 increment 5 — the billing skeleton (design doc §7).
--
-- plans: operator-curated tiers; `agendas` maps agenda key -> visible bool
-- (the admin "which agendas can each tier see" screen edits this). Plan
-- definitions are not secret: any authenticated user may read them; writes go
-- through the admin API only. Exactly one plan is the default (what an
-- account without an entitlements row gets).
--
-- entitlements: <=1 row per account, written ONLY by the service role (the
-- Stripe webhook + the admin comp screen); tenants read their own row via
-- RLS. `last_event_created` makes webhook processing out-of-order tolerant
-- (Stripe does not guarantee delivery order).
--
-- stripe_webhook_events: the idempotency ledger — INSERT .. ON CONFLICT
-- DO NOTHING on the Stripe event id is the atomic already-processed gate
-- (A9: never check-then-act). Service-role only.

begin;

create table if not exists plans (
  key        text primary key,
  name       text not null,
  position   int  not null default 0,
  agendas    jsonb not null default '{}'::jsonb,
  is_default boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create unique index if not exists plans_one_default on plans (is_default) where is_default;

insert into plans (key, name, position, agendas, is_default)
values ('free', 'Free', 0,
        '{"browse": true, "pipeline": true, "estimations": true, "watchdogs": true,
          "notifications": true, "brokers": true, "collections": true}'::jsonb,
        true)
on conflict (key) do nothing;

create table if not exists entitlements (
  account_id             uuid primary key references accounts(id) on delete cascade,
  plan                   text not null references plans(key),
  status                 text not null default 'active'
                           check (status in ('active', 'trialing', 'past_due', 'canceled')),
  stripe_customer_id     text,
  stripe_subscription_id text,
  current_period_end     timestamptz,
  last_event_created     bigint,
  created_at             timestamptz not null default now(),
  updated_at             timestamptz not null default now()
);

create table if not exists stripe_webhook_events (
  event_id    text primary key,
  type        text not null,
  created     bigint not null,
  payload     jsonb,
  received_at timestamptz not null default now()
);

alter table plans                 enable row level security;
alter table entitlements          enable row level security;
alter table stripe_webhook_events enable row level security;

revoke all on plans, entitlements, stripe_webhook_events from anon, authenticated;
grant select on plans to authenticated;
grant select on entitlements to authenticated;

create policy plans_read_all on plans
  for select to authenticated
  using (true);

create policy entitlements_read_own on entitlements
  for select to authenticated
  using (account_id in (select current_account_ids()));

commit;
