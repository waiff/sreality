# Public release — accounts, multi-tenancy, billing

Taking the single-operator platform public: user accounts, per-account private state,
email+Google login, Stripe billing, admin-gated internals, shared scraped market data
common to all. Full plan, sequencing, and gates: `docs/design/public-release-program.md`
(index) → `phase-0-emergency-hardening.md`, `phase-1-multitenancy-foundations.md`
(Amendments A1–A10), `waves-1-4-public-features.md`.

## Status

- **Phase 0 (emergency hardening)** — designed, **not yet applied**. Closes 3 live
  anon-exploitable criticals (25 RLS-off tables, ~30 write-through `*_public` views, the
  broker-PII default-ACL exposure). Independent of the SaaS decision; should ship regardless.
- **Phase 1 (multi-tenant foundations)** — in progress.
  - Increment 1 ✅ — accounts/account_members/admins, `current_account_ids()` /
    `is_platform_admin()`, the on-signup handler, JWT verify (JWKS/ES256) (migrations
    286+287, PR #747). Google OAuth + Railway `SUPABASE_URL` configured by the operator.
  - Increment 2 ✅ — login made visible: account menu (sign-in / signed-in-as / logout)
    in the app Shell. Purely additive, no route gated yet.
  - Increment 3 🟡 — the tenant DB pool + `account_id`/RLS across the ~18 user-state
    tables + the pipeline PK rewrite (property_pipeline → `(account_id, property_id)`) +
    a real-Postgres CI lane proving cross-tenant denial. Security-critical; verified against
    the live DB with two accounts before anything nears prod.
  - Increments 4–5 (admin gating, Stripe + entitlements + tier-visibility settings) — not started.
- **Waves 1–4 (public features)** — not started; gated on Phase 1's exit (RLS lane green +
  external re-audit + 2-account pen-test).

## Next

- Finish increment 3 (see design doc §"Workstream sequencing"), then increments 4–5.
- Apply Phase 0 before any wave that widens the anon/authenticated surface further.
