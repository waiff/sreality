"""W1-b: migration 497 makes the 19-column write legal on the OLD table.

THE FAILURE THIS EXISTS FOR. A merge deploys Railway and the next hourly
`location_claims_intake.yml` tick within minutes; a migration is applied by hand. So new
code runs on the old schema for a window of unknown length, and the first cut of this wave
shipped one destructive migration for after the rollout — which left the 19-column INSERT
facing an old table that still demanded five columns it no longer writes:

  * `source_id_native`, `extractor_id`, `extractor_version` — NOT NULL, no default -> 23502
  * `snapshot_anchor` NOT NULL DEFAULT 'snapshot' + `loc_claim_anchor`, which then demands
    `snapshot_id` -> 23514
  * `loc_claim_text_evidence` (7 live `regex_text` entries), `loc_claim_legacy` (24 live
    `legacy_column` entries), `loc_claim_distance_shape` -> 23514

Every hourly claim write would have failed until someone applied the drops, and no ordering
of ONE migration fixes it: applied first, the OLD code hits 42703 instead.

So the wave is two files — 497 RELAXES before the merge, 498 DROPS after the rollout — and
this gate is what stops the window being reintroduced. It does not transcribe the list: it
DERIVES it from migration 382's `create table location_claims`, so a NOT NULL column or a
CHECK added to the claim spine later is held to the same rule the day it lands.

The FK half is checked by name (`location_claims.payload_id -> portal_raw_payloads`): with
the claim arms gone from `payloads._REPIN_SQL`, the retention DELETE starts evicting bodies
that FK still points at, and a NO ACTION FK turns that into a 23503 that rolls back the
whole bounded append transaction on every scraper detail fetch in the group.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.location_data.test_claims_slim_migration import KEPT_COLUMNS, _dropped_columns

_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
_CREATE = (_MIGRATIONS / "382_location_w1_claims.sql").read_text(encoding="utf-8")
_RELAX = (_MIGRATIONS / "497_location_w1b_claims_relax.sql").read_text(encoding="utf-8")
_SLIM = (_MIGRATIONS / "498_location_w1b_claims_slim.sql").read_text(encoding="utf-8")


def _claims_table_body() -> str:
    body = _CREATE.split("create table location_claims (")[1].split("\n);")[0]
    return re.sub(r"--[^\n]*", "", body)


def _top_level_items(body: str) -> list[str]:
    items, depth, current = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            items.append("".join(current))
            current = []
        else:
            current.append(ch)
    items.append("".join(current))
    return [" ".join(i.split()) for i in items if i.strip()]


def _compulsory_columns() -> set[str]:
    """Columns 382 declares NOT NULL, whatever their default.

    A default does not make a column safe on its own: `snapshot_anchor`'s fires and then
    trips `loc_claim_anchor`. The default-carrying ones are separated below.
    """
    out = set()
    for item in _top_level_items(_claims_table_body()):
        words = item.split()
        if not words or words[0] in ("unique", "check", "primary", "foreign", "constraint"):
            continue
        if "not null" in item.lower():
            out.add(words[0])
    return out


def _table_checks() -> dict[str, str]:
    """Named table-level CHECK constraints on `location_claims`, by name."""
    out = {}
    for item in _top_level_items(_claims_table_body()):
        m = re.match(r"constraint ([a-z0-9_]+) check", item, re.I)
        if m:
            out[m.group(1)] = item.lower()
    return out


def _relaxed_not_nulls() -> set[str]:
    return set(re.findall(r"alter column ([a-z0-9_]+)\s+drop not null", _RELAX))


def _relaxed_constraints() -> set[str]:
    return set(re.findall(r"drop constraint if exists ([a-z0-9_]+)", _RELAX))


def _statements(sql: str) -> str:
    """The file with its comments stripped — the verb scan below must read SQL, not prose."""
    return re.sub(r"--[^\n]*", "", sql).lower()


# A CHECK naming a dropped column is a window failure UNLESS it stays true when that column
# is always NULL. Two do, and only these two: the rule below refuses any third without a
# human deciding which side it is on.
_WINDOW_SAFE_CHECKS = {
    "loc_claim_value_present":
        "a disjunction over the five value slots — the new write always populates at least "
        "one of the four KEPT ones, so losing value_shape cannot falsify it. 498 re-states "
        "it minus that column; dropping it early would leave the window unguarded.",
    "loc_claim_evidence_payload":
        "`evidence_quote is null or payload_sha256 is not null` — the new write never "
        "supplies a quote, so the left arm is always true.",
}


# ---------------------------------------------------------------- the window


def test_every_compulsory_column_the_write_omits_is_relaxed_first():
    """DERIVED, not transcribed: any NOT NULL column of the claim spine that the 19-column
    write does not supply must lose its NOT NULL in 497, or the hourly intake writes zero
    claims between the merge and 498."""
    written = KEPT_COLUMNS - {"id"}           # id is the serial the INSERT never names
    defaulted = {"extracted_at", "page_kind", "legacy_write_path_unknown", "created_at",
                 "blur_evidence", "snapshot_anchor"}
    unmet = _compulsory_columns() - written - defaulted - _relaxed_not_nulls()
    assert not unmet, (
        "migration 497 must DROP NOT NULL on every compulsory column the 19-column write "
        f"omits, or window (A) is an outage: {sorted(unmet)}")
    # The three that actually bite, spelled out so a future edit cannot quietly shrink the
    # derived set to nothing and still pass.
    assert {"source_id_native", "extractor_id", "extractor_version"} <= _relaxed_not_nulls()


def test_snapshot_anchor_loses_both_its_not_null_and_its_default():
    """The one column a default makes WORSE. 'snapshot' fires on an omitted column and
    `loc_claim_anchor` then demands a snapshot_id the new write never has; and for as long
    as the column survives, a default-stamped anchor is a wrong fact rather than a NULL."""
    assert "snapshot_anchor" in _relaxed_not_nulls()
    assert re.search(r"alter column snapshot_anchor\s+drop default", _RELAX)
    assert "loc_claim_anchor" in _relaxed_constraints()


def test_every_check_naming_a_dropped_column_is_relaxed_first():
    """Same derivation on the CHECK side: a table CHECK that references a column 498 drops
    cannot survive the window, because the new write leaves that column NULL."""
    dropped = _dropped_columns()
    relaxed = _relaxed_constraints()
    offenders = []
    for name, body in _table_checks().items():
        named = {c for c in dropped if re.search(rf"\b{c}\b", body)}
        if not named:
            continue
        if name in relaxed or name in _WINDOW_SAFE_CHECKS:
            continue
        offenders.append(f"{name} names {sorted(named)}")
    assert not offenders, (
        "migration 497 must drop every CHECK that references a column 498 drops — or the "
        "CHECK must be added to _WINDOW_SAFE_CHECKS with the argument for why it stays "
        "true when that column is always NULL:\n  " + "\n  ".join(offenders))
    for name in ("loc_claim_anchor", "loc_claim_text_evidence", "loc_claim_legacy",
                 "loc_claim_distance_shape", "loc_claim_llm_model"):
        assert name in relaxed, name


def test_the_window_safe_exemptions_are_exactly_these_two():
    """The escape hatch above is the one place this gate can be widened, so it is pinned.
    A third CHECK cannot join it by accident — someone has to write down why."""
    assert set(_WINDOW_SAFE_CHECKS) == {"loc_claim_value_present",
                                        "loc_claim_evidence_payload"}
    assert all(len(reason) > 40 for reason in _WINDOW_SAFE_CHECKS.values())


def test_the_invariants_worth_keeping_survive_the_window():
    """497 relaxes; it does not gut. Every claim the new write emits satisfies these, so
    dropping them early would buy nothing and lose a window's worth of guard."""
    for name in ("loc_claim_value_present", "loc_claim_coordinate_shape"):
        assert name not in _relaxed_constraints(), name


def test_the_payload_fk_goes_before_the_pin_predicate_stops_pinning():
    """`payloads._REPIN_SQL` no longer pins bodies a claim points at, so `_PRUNE_SQL`
    starts evicting rows `location_claims.payload_id` still references. NO ACTION makes
    that a 23503 that rolls back the whole bounded append transaction — one WARN line per
    detail fetch, an archive that silently stops growing, and a leaked R2 object."""
    assert "location_claims_payload_id_fkey" in _relaxed_constraints()
    # NOT the same object as migration 403's index of a very similar name.
    assert "drop index" not in _RELAX.lower()


def test_the_relax_migration_only_relaxes():
    """It runs BEFORE the code, so it must be a no-op for the OLD code too: nothing
    created, no column or table dropped, no data touched."""
    body = _statements(_RELAX)
    for verb in ("create ", "drop column", "drop table", "drop view", "insert into",
                 "update ", "delete from", "add constraint", "set not null"):
        assert verb not in body, verb


def test_the_two_files_state_their_order():
    """An operator who applies them in the wrong order gets window (B) instead of (A).
    Each file has to say so where it will be read — at the top, in psql."""
    for text in (_RELAX, _SLIM):
        head = text[:4000].lower()
        assert "497" in head and "498" in head
    assert "before" in _RELAX[:4000].lower()
    assert "after" in _SLIM[:4000].lower()
    # And the destructive half repeats the one operational rule it cannot enforce.
    assert "--retract" in _SLIM[:4000]
