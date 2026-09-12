"""Admissibility — the ONE gate, evaluated once and handed to every step.

`bind.admissible()` refuses four classes: `subject_scoped=false` (the remax carousel),
`licence_class='ephemeral_display_only'` (Mapy), a portal-proprietary identifier, and a
claim the normalizer rejected. It used to live in the survivorship evaluator, which applied
it — while candidate generation and position assignment did not. So the carousel street
ranked a candidate, that candidate carried the admin chain, and the preserve-if-null
registry fill wrote the poisoned address back out as `registry_derived`.

Refused means "may not win", never "discarded": the claim row is still in
`location_claims`, and W2-a additionally puts the licence half of the gate in the claim
SELECT itself, so a Mapy-class coordinate is not refused at the winner — it is never read.
"""

from __future__ import annotations

from datetime import datetime, timezone

from location_data.resolver import bind as step_bind
from location_data.resolver import core, normalize, resolve_db
from location_data.resolver.types import Claim
from location_data.resolver.version import RESOLVER_VERSION
from tests.location_data import mini_mirror as mm

# The Prague address the mini-mirror carries end to end.
STREET = "Nad Bořislavkou 487/40"


def _resolve(claims):
    return core.resolve(
        claims, mm.context(), resolver_version=RESOLVER_VERSION,
        registry_version="ruian:2026-07-31",
    )


def _reason(claim):
    return step_bind.admissible(claim, normalize.normalize_all([claim]).get(claim.id))


# ------------------------------------------------------------------- the four classes


def test_the_four_refusals_name_themselves():
    assert _reason(mm.claim(1, "street_name", value_text=STREET, subject_scoped=False)) == (
        "not_subject_scoped"
    )
    assert _reason(
        mm.claim(2, "coordinate", lat=50.0, lon=14.0, licence_class="ephemeral_display_only")
    ) == "licence_ephemeral"
    assert _reason(mm.claim(3, "portal_admin_id", value_text="5122")) == (
        "portal_proprietary_identifier"
    )
    assert _reason(mm.claim(4, "street_name", value_text=STREET)) is None


def test_the_licence_gate_is_in_the_claim_read_not_only_in_the_code():
    """Migration 384 spent three CHECK constraints and a `position_licence_class` column on
    each store of record to make a Mapy coordinate unstorable. `listing_location` has no such
    column because the guard moved upstream and got stronger: the resolver's own projection
    refuses to SELECT one."""
    for sql in (resolve_db._CLAIMS_SELECT, resolve_db._CLAIMS_SQL, resolve_db._CLAIMS_BULK_SQL):
        assert "c.licence_class in ('portal', 'operator')" in " ".join(sql.split()).lower()


def test_only_an_active_contracts_claims_are_read():
    """`location_claims` is append-only and its fingerprint hashes `extractor_version`, so a
    contract BUMP inserts new rows beside the old ones rather than superseding them — and the
    superseded row has the LOWER id, so it would win every "first admissible claim of this
    type" tie. W1-c bumped all nine contracts at once, which makes that the normal case on
    any listing whose body has not changed since.

    Filtering at READ keeps the evidence on disk and makes W2-b's delete a cleanup rather
    than a correctness step. `is_active` lives on the contract HEADER, one per source."""
    for sql in (resolve_db._CLAIMS_SELECT, resolve_db._CLAIMS_SQL, resolve_db._CLAIMS_BULK_SQL):
        flat = " ".join(sql.split()).lower()
        assert "join portal_contracts pc on pc.id = pce.contract_id" in flat, flat
        assert "where pce.id = c.contract_entry_id and pc.is_active" in flat, flat
        # Operator claims carry no entry by construction and are named EXPLICITLY — a
        # NULL-tolerant join would also admit a portal claim that lost its entry id.
        assert "c.contract_entry_id is null and c.licence_class = 'operator'" in flat, flat


class _ClaimCursor:
    """A fake that OBEYS the claim projection's two predicates instead of ignoring them.

    It refuses a statement that does not carry them and then applies them to its own rows, so
    the test below goes red BOTH when the predicate is dropped from the SQL and when the
    loader stops using that SQL.
    """

    #  id, listing_id, licence_class, contract_entry_id, entry_is_active
    ROWS = (
        (1, 77, "portal", 10, True),    # the live contract's claim
        (2, 77, "portal", 9, False),    # the SAME fact, from the superseded version
        (3, 77, "operator", None, None),  # an operator correction, no entry at all
        (4, 77, "ephemeral_display_only", 10, True),  # a Mapy coordinate
        (5, 77, "portal", None, None),  # a portal claim that lost its entry id
    )

    def __init__(self) -> None:
        self.result: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def execute(self, sql, params=None):
        flat = " ".join(sql.split()).lower()
        assert "c.licence_class in ('portal', 'operator')" in flat, "licence rail missing"
        assert "and pc.is_active" in flat, "active-contract rail missing"
        assert "c.contract_entry_id is null and c.licence_class = 'operator'" in flat
        self.result = [
            _row(claim_id, listing_id)
            for claim_id, listing_id, licence, entry, active in self.ROWS
            if licence in ("portal", "operator")
            and ((entry is None and licence == "operator") or active is True)
        ]

    def fetchall(self):
        return self.result


def _row(claim_id: int, listing_id: int) -> tuple:
    return (claim_id, listing_id, "sreality", "obec_name", "api_json",
            "portal_structured_field", "portal", mm._T0, "Praha", None, None, None,
            {}, None, None, "none", "high", True)


class _ClaimConn:
    def __init__(self) -> None:
        self.cur = _ClaimCursor()

    def cursor(self):
        return self.cur


def test_a_retired_contract_entrys_claim_is_never_loaded():
    """Five rows for one listing; three are inadmissible and only two reach the resolver."""
    loaded = resolve_db.load_claims_bulk(_ClaimConn(), [77])
    assert [c.id for c in loaded[77]] == [1, 3]


# --------------------------------------------------------------- the carousel street


def test_a_carousel_street_never_wins_the_street_field():
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text=STREET),
        mm.claim(3, "street_name", value_text="Milady Horákové 12", subject_scoped=False),
    ])
    assert resolution.street_name == "Nad Bořislavkou"


def test_a_carousel_street_never_ranks_a_candidate_or_carries_the_admin_chain():
    """With NO admissible street claim the listing must resolve at the obec rung, not at
    whatever address the carousel happened to name."""
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text=STREET, subject_scoped=False),
        mm.claim(3, "house_number_cp", value_text="487", subject_scoped=False),
    ])
    assert resolution.street_name is None
    assert resolution.ruian_adm_kod is None
    assert resolution.granularity == "obec"


def test_a_carousel_coordinate_never_becomes_the_pin():
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "coordinate", lat=49.5936, lon=17.2987, subject_scoped=False),
        mm.claim(3, "coordinate", lat=50.0755, lon=14.4378),
    ])
    assert (resolution.lat, resolution.lon) == (50.0755, 14.4378)


# --------------------------------------------------------- typed slots, not verbatim


def test_a_combined_house_number_claim_is_unwrapped_into_its_own_slot():
    """Three typed slots, never collapsed. Keying the unwrap on which slot happens to be
    PRESENT wrote "487/40" into house_number_cp verbatim, because a house-number claim
    carries no `street`/`psc` slot to trip the old branch."""
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "house_number_cp", value_text="487/40"),
    ])
    assert resolution.house_number_cp == "487"


def test_the_orientation_number_keeps_its_letter():
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "house_number_co", value_text="487/40a"),
    ])
    assert resolution.house_number_co == "40a"


# ------------------------------------------------------------ purity: no local timezone


def test_a_naive_and_an_aware_claim_set_resolve_identically():
    """`datetime.timestamp()` on a naive value silently applies the HOST's timezone, so the
    same claims would hash differently on two machines."""

    def _claims(tzinfo):
        moment = datetime(2026, 8, 1, 12, 0, tzinfo=tzinfo)
        return [
            Claim(
                id=i, listing_id=900001, source="sreality", claim_type=claim_type,
                surface="api_json", extraction_method="portal_structured_field",
                extractor_id="fx", licence_class="portal", observed_at=moment,
                value_text=value, claim_confidence="high", subject_scoped=True,
            )
            for i, (claim_type, value) in enumerate(
                (("obec_name", "Praha"), ("street_name", STREET)), start=1
            )
        ]

    naive = _resolve(_claims(None))
    aware = _resolve(_claims(timezone.utc))
    assert naive.claim_set_hash == aware.claim_set_hash
    assert naive.street_name == aware.street_name
    assert naive.granularity == aware.granularity
