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
    for sql in (resolve_db._CLAIMS_SQL, resolve_db._CLAIMS_BULK_SQL):
        assert "c.licence_class in ('portal', 'operator')" in " ".join(sql.split()).lower()


def test_the_claim_read_takes_a_listings_newest_evidence_not_only_the_active_contract():
    """W11, incident 2026-09-14. W1-c spelled the version rail `pc.is_active`, so the hours
    between a contract BUMP and its re-mine judged a listing with no claims at all: 595,816
    rows came out `unknown/undetermined/low` and Browse fell to 45,810 of ~350,000.

    The rail now reads the highest contract version PRESENT for that (listing, portal) that
    is `<= the active one`, which is the active version's claims the moment they exist."""
    for sql in (resolve_db._CLAIMS_SQL, resolve_db._CLAIMS_BULK_SQL):
        flat = " ".join(sql.split()).lower()
        assert "join portal_contracts pc on pc.id = pce.contract_id" in flat, flat
        assert "join portal_contracts act on act.source = pc.source and act.is_active" in flat
        assert "pc.version <= act.version" in flat, flat
        # ONE version per listing per portal, never a mix of two contracts' halves.
        assert ("max(pc.version) over (partition by c.listing_id, c.source) "
                "as newest_version") in flat, flat
        assert "contract_version is null or contract_version = newest_version" in flat, flat
        # Operator claims carry no entry by construction and are named EXPLICITLY — a
        # NULL-tolerant join would also admit a portal claim that lost its entry id.
        assert "c.contract_entry_id is null and c.licence_class = 'operator'" in flat, flat


def test_the_w11_rule_carries_its_own_resolver_version():
    """A rule that can change an output must move `RESOLVER_VERSION`, or the sweep's version
    arm never re-queues the rows the old rule got wrong — which here is 595,816 of them."""
    assert RESOLVER_VERSION == "resolver:v5.1"


class _ClaimCursor:
    """A fake that OBEYS the claim projection's rails instead of ignoring them.

    It refuses a statement that does not carry them and then applies them to its own rows, so
    the tests below go red BOTH when a rail is dropped from the SQL and when the loader stops
    using that SQL. `sreality` is at version 4 and `bazos` at 6 (the live 2026-09-14 heads).
    """

    ACTIVE = {"sreality": 4, "bazos": 6}

    #  id, listing_id, source, licence_class, contract version (None = no entry)
    ROWS = (
        (1, 77, "sreality", "portal", 4),    # the active version's claim
        (2, 77, "sreality", "portal", 3),    # the SAME fact, from the superseded version
        (3, 77, "operator", "operator", None),  # an operator correction, no entry at all
        (4, 77, "sreality", "ephemeral_display_only", 4),  # a Mapy coordinate
        (5, 77, "sreality", "portal", None),  # a portal claim that lost its entry id
        # A LISTING THE BUMP OUTRAN: nothing under bazos@6 yet, so @5 is its newest
        # evidence — and @4 beside it must NOT be mixed in.
        (6, 88, "bazos", "portal", 5),
        (7, 88, "bazos", "portal", 4),
        (8, 88, "operator", "operator", None),
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
        assert "pc.version <= act.version" in flat, "active-version ceiling missing"
        assert "newest_version" in flat, "newest-evidence rail missing"
        assert "c.contract_entry_id is null and c.licence_class = 'operator'" in flat
        admissible = [
            r for r in self.ROWS
            if r[3] in ("portal", "operator")
            and ((r[4] is None and r[3] == "operator")
                 or (r[4] is not None and r[4] <= self.ACTIVE[r[2]]))
        ]
        newest = {}
        for _, listing, source, _, version in admissible:
            if version is not None:
                newest[(listing, source)] = max(newest.get((listing, source), 0), version)
        self.result = [
            _row(r[0], r[1], r[2])
            for r in admissible
            if r[4] is None or r[4] == newest[(r[1], r[2])]
        ]
        self.result.sort(key=lambda row: (row[1], row[0]))

    def fetchall(self):
        return self.result


def _row(claim_id: int, listing_id: int, source: str = "sreality") -> tuple:
    return (claim_id, listing_id, source, "obec_name", "api_json",
            "portal_structured_field", "portal", mm._T0, "Praha", None, None, None,
            {}, None, None, "none", "high", True)


class _ClaimConn:
    def __init__(self) -> None:
        self.cur = _ClaimCursor()

    def cursor(self):
        return self.cur


def test_a_superseded_versions_claim_is_never_loaded_beside_the_active_ones():
    """Five rows for one listing; three are inadmissible and only two reach the resolver."""
    loaded = resolve_db.load_claims_bulk(_ClaimConn(), [77])
    assert [c.id for c in loaded[77]] == [1, 3]


def test_a_listing_the_bump_outran_still_reads_its_newest_earlier_version():
    """bazos@6 is active and this listing has nothing under it yet. It reads @5 — its newest
    evidence — and never @4 beside it, so the answer is one contract's, not two halves."""
    loaded = resolve_db.load_claims_bulk(_ClaimConn(), [88])
    assert [c.id for c in loaded[88]] == [6, 8]


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
