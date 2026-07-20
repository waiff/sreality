# Public-release program — index

**Goal:** take the single-operator platform to a public multi-tenant SaaS — user accounts, per-account private state, email+Google login, Stripe billing, admin-gated internals, shared scraped market data common to all, released as four feature waves.

**Verdict (from the feasibility audit):** feasible — the account model is the easy part; the gates are the live security posture and GDPR/scraping compliance. Full analysis: the feasibility & security dossier (Artifact). This folder holds the implementation-grade designs.

**Status: design/planning only. Nothing here is applied.** No migration has been run, no production code merged. Every design was adversarially reviewed; the reviews fed corrections back into the specs (notably Phase 1 Amendments A1–A8).

## The documents

| Phase / wave | Design doc | One-line |
| --- | --- | --- |
| **Phase 0 — Emergency hardening** | `phase-0-emergency-hardening.md` | Close the 3 live anon-exploitable criticals now (revoke anon writes, fix the default ACL, RLS-enable 25 tables, lock 8 functions) + fail-closed API + a CI grant-gate. Ships alone, independent of the SaaS decision. |
| **Phase 1 — Multi-tenant foundations** | `phase-1-multitenancy-foundations.md` | The one large lift: Supabase Auth, `accounts`/`account_members`, RLS via `current_account_ids()`, a fail-closed non-superuser tenant pool, admin gating, Stripe skeleton, anti-abuse, the RLS test lane, a day-daily-safe rollout — plus **Amendments A1–A8** the wave reviews surfaced. |
| **Waves 1–4 — Public features** | `waves-1-4-public-features.md` | Wave 1 extension + agent estimations (metered flagship); Wave 2 pipeline; Wave 3 watchdogs + notifications; Wave 4 broker analytics (legal-gated). |
| **Post-ship remediation (2026-07-20)** | `public-release-remediation-2026-07.md` | Execution spec for the live-verified review of the deployed 316–319 batch: R1 P0 hotfix (Browse estimates / golden-set / Health matviews), R2 grant+matview hardening, R3 test-lane + standing CI gate, R4 broker-fetch polish. |

## Sequencing & gates

```
Phase 0 (ship now, unconditional)
   └─► Phase 1 (foundations + A1–A8) ──[exit gate: RLS lane green + external re-audit + 2-account pen-test]
          ├─► Wave 1  extension + agent estimations   [gate: re-audit + CWS review]
          ├─► Wave 2  opportunity pipeline             [gate: re-audit + 2-account pen-test]  (ext. writes need Wave 1 session)
          ├─► Wave 3  watchdogs + notifications        [gate: re-audit + GDPR/deliverability sign-off]
          └─► Wave 4  broker analytics                 [gate: LEGAL sign-off + broker-PII re-audit]
```

The security audit is **embedded** (the RLS/grants inventory *is* the feasibility work) and **gated** (a formal external re-audit at the Phase-1 exit and before each surface-widening wave), not a separate before/after project. A real-Postgres CI lane asserts cross-tenant denial continuously — the `_FakeConn` test harness can't see policies.

## Settled operator decisions (2026-07-10)

1. **Fully login-gated** — only a static marketing page is anonymous; `anon` revoked to ~nothing; shared-market views re-granted `anon`→`authenticated` (**except** broker-PII surfaces — see Phase 1 A6).
2. **Accounts + members from day one** — teams/seats are additive later via the `current_account_ids()` helper with zero policy changes.

Still open (do not block the build): Wave-4 broker-contact masking level (firm-level vs masked vs raw), and the EU VAT approach (recommend merchant-of-record first).

## The cross-cutting corrections (Phase 1 Amendments A1–A10)

The wave designs stress-tested Phase 1 and found ten foundation fixes — the highest-value output of the exercise. In `phase-1-multitenancy-foundations.md`: the tenant pool needs an explicit one-transaction-per-request contract (A1); the merge reconciler runs `BYPASSRLS` and must be account-partitioned (A2); both `pipeline_stages` global uniques re-key per account (A3); `estimation_runs.account_id` is nullable + stamped synchronously (A4); shared-market base tables need explicit `authenticated` read policies (A5); **broker-PII surfaces are excluded from the re-grant until Wave 4 masks them** (A6); `channel_sends` stays a service-role ledger (A7); detection stays co-hostable in the API (A8); every quota/concurrency/idempotency gate is atomic, never check-then-act (A9); long/paid work drains off the request process onto the worker (A10).

**Recurring theme across all four wave reviews:** authentication (`verify_jwt`) is not authorization — every per-account route must run on the **tenant pool** so RLS scopes the rows, not merely be JWT-gated. Wave 1's `GET/PATCH /estimations/{id}` (IDOR) and Wave 2's `portal_lookup` both tripped on this. It's the single most important implementation rule for the whole program.
