-- 543_lifecycle_description_truth.sql — a delisting is not a sale (sold-comps W1).
--
-- Migration 191 seeded `app_settings.default_lifecycle` with a description calling
-- "delisted" (is_active=false) "closed deals, a transacted-price proxy". It is neither:
-- `is_active=false` means the ADVERTISEMENT ended (migration 453's header, on 70,130
-- long-unseen rows: not "probably sold": unknown). The listing may have been withdrawn,
-- re-listed elsewhere, or closed by a walk that simply never saw it.
--
-- The same sentence was carried in three code comments, and this PR deletes it there.
-- THIS row is the fourth and the only operator-facing one: `GET /admin/settings` selects
-- `description` and the SPA renders it as the info hint beside the key on /settings. Left
-- alone, the app would keep asserting exactly what the rest of the PR retracts.
--
-- Data-only and idempotent: one UPDATE keyed on the setting, no schema change, and the
-- stored `value` is untouched — so nothing the estimator reads changes, and the migration
-- 020 history trigger (which fires on a value change only) correctly does not fire. The
-- superseded text stays readable where it was written, in migration 191.

set lock_timeout = '5s';

update app_settings
   set description =
         'Default cohort lifecycle for comparable lookups. One of "active" '
         '(is_active=true plus the max_age_days freshness window), "delisted" '
         '(is_active=false — the advertisement ended, which is not a sale and '
         'carries no transacted price; registered sales are their own store, '
         'sold_transactions), or "all" (no is_active gate). The agent inherits '
         'this as the round-1 base and can override per round via '
         'find_comparables_relaxed.lifecycle.'
 where key = 'default_lifecycle';

reset lock_timeout;
