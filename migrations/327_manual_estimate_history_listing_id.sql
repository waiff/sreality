-- 327_manual_estimate_history_listing_id.sql
-- R2 Phase A2 of the listing-identity refactor
-- (docs/design/listing-identity-r2-pk-swap-runbook.md § 2 A2).
-- Carries the surrogate into manual_rental_estimates_history.
--
-- This carrier has no Python writer to patch: migration 046 fills it from a
-- BEFORE UPDATE/DELETE trigger on manual_rental_estimates, copying the OLD row.
-- So its dual-write comes free from the parent's — the function just has to copy
-- one more column. Redefining the function is enough; both triggers
-- (_history_update, _history_delete) call it by name and are untouched.
--
-- Everything else in the function is byte-for-byte migration 046's definition.

CREATE OR REPLACE FUNCTION manual_rental_estimates_record_history()
RETURNS trigger
LANGUAGE plpgsql
AS $$
begin
  insert into manual_rental_estimates_history (
    estimate_id, sreality_id, listing_id, rent_czk, author, source_kind, notes,
    change_kind, replaced_at, replaced_by
  ) values (
    old.id, old.sreality_id, old.listing_id, old.rent_czk, old.author,
    old.source_kind, old.notes,
    case when TG_OP = 'DELETE' then 'delete' else 'update' end,
    now(), old.updated_by
  );
  if TG_OP = 'DELETE' then
    return old;
  else
    return new;
  end if;
end;
$$;
