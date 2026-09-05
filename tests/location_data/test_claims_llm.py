"""W2-10: the bazos free-text location lane, tested hermetically.

No DB, no network, no clock. The model's answer is CANNED and the gazetteer is a fake, so
every assertion here is about the lane's own rules: what it claims, what it refuses, and
what it records when it refuses.

WHY THIS FILE IS THE ONLY COVERAGE THIS LANE HAS. `test_contract_fixture_diff.py`'s
archived arm filters on `ARCHIVE_READERS`, and this lane's reader is in `LLM_READERS`, so
the golden files will be green while proving nothing about it. Do not read a green golden
as coverage of the free-text lane — read this module.

The entries below are built BY HAND rather than loaded, so the hermetic assertions stay
readable and independent of a contract edit. bazos@3 has since shipped the real ones, and
`test_every_bazos_llm_entry_is_executable` loads those off disk and asserts the two sets are
identical — which is what keeps this file's fixtures from drifting away from what production
actually executes.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from location_data import claims_intake, contracts
from location_data.claims_intake import Entry, IntakeRefused, ListingRow
from location_data.claims_llm import (
    BLOCK_ORDER,
    FIELD_CLAIM_TYPES,
    FIELD_ORDER,
    LLM_READERS,
    LOCATION_TOOL,
    MAX_TOKENS,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    ArchivedPayload,
    Refusal,
    _candidate_obec,
    _validated_groups,
    block_texts,
    build_user_message,
    estimated_cost_usd,
    extract_payload,
    gazetteer_refusal,
    llm_entries,
    parse_field_answer,
    resolve_model,
)
from location_data.claims_remine_archive import ARCHIVE_READERS
from location_data.html_scope import ScopeRegister, scope_html
from tests.location_data.claim_intake_fixtures import entries_for
import location_data.claims_llm as claims_llm

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_382 = (_ROOT / "migrations" / "382_location_w1_claims.sql").read_text("utf-8")
_LANE_SOURCE = (_ROOT / "location_data" / "claims_llm.py").read_text("utf-8")
_LANE_AST = ast.parse(_LANE_SOURCE)
_BAKEOFF_PATH = _ROOT / "scripts" / "location_llm_bakeoff.py"
_BAKEOFF_AST = ast.parse(_BAKEOFF_PATH.read_text("utf-8"))

FETCHED_AT = datetime(2026, 9, 4, 6, 30, tzinfo=UTC)
FIXTURE = (_ROOT / "tests" / "fixtures" / "location_w2" / "bazos_detail.html").read_bytes()

# The bazos register as the shipped contract declares it (contracts/portals/bazos.yaml).
BAZOS_ZONES = (
    {"locator_kind": "html_selector", "locator": {"css": ".podobne, #podobne"}},
    {"locator_kind": "html_selector", "locator": {"css": "footer, .hlavicka"}},
)
REGISTER = ScopeRegister.from_zones("bazos", BAZOS_ZONES)

DESCRIPTION_CSS = "div.popisdetail"
TITLE_CSS = "h1.nadpisdetail"


# ------------------------------------------------------------------ builders

def entry(
    entry_id: str, *, block: str, field: str, claim_type: str,
    transform: tuple[str, ...] = (), css: str | None = None, page_kind: str = "detail",
    reader: str = "llm_location_text", entry_pk: int = 7100,
) -> Entry:
    surface = "description" if block == "description" else "html_selector"
    return Entry(
        id=entry_pk, source="bazos", contract_id=3, contract_version=3, entry_id=entry_id,
        surface=surface, page_kind=page_kind,
        locator={"reader": reader,
                 "css": css or (DESCRIPTION_CSS if block == "description" else TITLE_CSS),
                 "llm_block": block, "llm_field": field},
        claim_type=claim_type, extraction_method="llm_text",
        subject_scope={"subject_scoped": True}, transform=transform, precision_map={},
        default_blur_evidence="none", default_licence_class="portal",
        cardinality="one", guards=())


def street_entries() -> list[Entry]:
    return [
        entry("bzs.desc.street", block="description", field="street",
              claim_type="street_name", entry_pk=7101),
        entry("bzs.title.street", block="title", field="street",
              claim_type="street_name", entry_pk=7102),
    ]


def full_entries() -> list[Entry]:
    """The sixteen entries bazos@3 declares, built by hand (eight output fields x two
    blocks). Pinned against the shipped contract by
    `test_every_bazos_llm_entry_is_executable`."""
    made: list[Entry] = []
    pk = 7200
    plan: list[tuple[str, str, tuple[str, ...]]] = [
        ("obec", "obec_name", ()),
        ("cast_obce", "cast_obce_name", ()),
        # `psc_normalise` keeps the five-digit run only, so a stated "186 00" lands
        # byte-identical to what `bzs.det.psc` and `bzs.det.legacy_psc` write and the three
        # agree on `value_norm` instead of contradicting each other on punctuation.
        ("psc", "psc", ("psc_normalise",)),
        ("street", "street_name", ()),
        ("house_number", "house_number_cp", ("split_cp_co:cp",)),
        ("house_number", "house_number_co", ("split_cp_co:co",)),
        ("landmark", "landmark", ()),
        ("lokalita_line", "address_line_verbatim", ()),
    ]
    for block in BLOCK_ORDER:
        for field, claim_type, transform in plan:
            pk += 1
            made.append(entry(
                f"bzs.{'desc' if block == 'description' else 'title'}."
                f"{claim_type}", block=block, field=field, claim_type=claim_type,
                transform=transform, entry_pk=pk))
    return made


def listing_row(**overrides: Any) -> ListingRow:
    kwargs: dict[str, Any] = {
        "listing_id": 5150, "source": "bazos", "source_id_native": "220021475",
        "raw_json": {}, "lat": None, "lon": None, "observed_at": FETCHED_AT,
        "in_mapy_inventory": False,
        "legacy_columns": dict(claims_llm._DUMMY_LEGACY_COLUMNS),
    }
    kwargs.update(overrides)
    return ListingRow(**kwargs)


def payload(**overrides: Any) -> ArchivedPayload:
    kwargs: dict[str, Any] = {
        "id": 9100, "source": "bazos", "source_id_native": "220021475",
        "page_kind": "detail", "payload_sha256": "cd" * 32,
        "first_observed_at": FETCHED_AT, "body": FIXTURE,
    }
    kwargs.update(overrides)
    return ArchivedPayload(**kwargs)


def document(body: bytes = FIXTURE):
    return scope_html(body, register=REGISTER)


class FakeGazetteer:
    """The RÚIAN facts the fixture's Prague-8 ad needs, and nothing else. Anything not
    listed here is genuinely absent from the registry, which is the point."""

    def __init__(
        self, *, names: set[str] | None = None, obec_codes: dict[str, list[int]] | None = None,
        streets: dict[int, set[str]] | None = None,
        points: set[tuple[int, str | None, int | None, int | None]] | None = None,
        psc: dict[str, list[int]] | None = None,
    ) -> None:
        self.names = names if names is not None else {"praha 8", "karlin", "praha"}
        self.obec_codes = obec_codes if obec_codes is not None else {"praha 8": [554782]}
        self.streets = streets if streets is not None else {554782: {"sokolovska"}}
        self.points = points if points is not None else {
            (554782, "sokolovska", 234, None)}
        self.psc = psc if psc is not None else {"18600": [554782]}

    def name_exists(self, name_norm: str) -> bool:
        return name_norm in self.names

    def obec_codes_for_name(self, name_norm: str) -> list[int]:
        return list(self.obec_codes.get(name_norm, []))

    def street_in_obec(self, obec_kod: int, street_norm: str) -> bool:
        return street_norm in self.streets.get(obec_kod, set())

    def address_point_exists(self, *, obec_kod, street_norm, cp, co) -> bool:
        return (obec_kod, street_norm, cp, co) in self.points

    def obec_codes_for_psc(self, psc: str) -> list[int]:
        return list(self.psc.get(psc, []))


def block_answer(**fields: Any) -> dict[str, Any]:
    """A whole block, with every field the tool schema requires; unnamed ones are null."""
    out: dict[str, Any] = {}
    for name in FIELD_CLAIM_TYPES:
        out[name] = fields.get(name) or {"value": None, "quote": None,
                                         "confidence": "low"}
    return out


def stated(value: str, quote: str | None = None, confidence: str = "high") -> dict[str, Any]:
    return {"value": value, "quote": quote if quote is not None else value,
            "confidence": confidence}


def answer(*, description: dict[str, Any] | None = None,
           title: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"from_description": block_answer(**(description or {})),
            "from_title": block_answer(**(title or {}))}


def run_extract(entries: list[Entry], model_answer: dict[str, Any], **kwargs: Any):
    kwargs.setdefault("gazetteer", FakeGazetteer())
    kwargs.setdefault("model", "gpt-5-nano")
    doc = kwargs.pop("document", None) or document()
    return extract_payload(payload(), listing_row(), entries, model_answer,
                           document=doc, **kwargs)


# ------------------------------------------------------------------ lane identity

def test_the_lane_identifiers_are_the_declared_strings_and_collide_with_nobody():
    assert claims_llm.LANE == "location_claims_llm"
    assert claims_llm.JOB_NAME == "location_claims_llm"
    assert claims_llm.CONCURRENCY_GROUP == "location-llm"
    assert claims_llm.LLM_VERSION == "claims_llm@1"
    assert claims_llm.WAVE == "W2"
    for value in (claims_llm.LANE, claims_llm.JOB_NAME, claims_llm.LLM_VERSION):
        assert value not in (
            claims_intake.LANE, claims_intake.INTAKE_VERSION,
            "location_claims_remine", "location_claims_remine_archive",
            "claims_remine@1", "claims_remine_archive@1")
    # The lane's batch size is a BILL, not a scan — it must not inherit W1's 10k floor.
    assert claims_llm.MIN_BATCH_SIZE == 1
    assert claims_llm.MAX_BATCH_SIZE < claims_intake.MIN_BATCH_SIZE


def test_the_three_reader_registries_stay_separate_and_mirrored():
    assert claims_intake.LLM_ONLY_READERS == set(LLM_READERS)
    assert not set(LLM_READERS) & set(claims_intake.READERS)
    assert not set(LLM_READERS) & set(ARCHIVE_READERS)
    assert "llm_location_text" in contracts.READER_CONTRACTS
    spec = contracts.READER_CONTRACTS["llm_location_text"]
    assert spec.methods == {"llm_text"}
    assert spec.substrates == {"description", "html_selector"}
    assert spec.locator_keys == {"css", "llm_block", "llm_field"}
    # Declared consultation must match what the body actually does — declaring a guard the
    # runtime never evaluates is the defect class `_check_executable` exists to catch.
    body = _reader_body("llm_location_text")
    calls = _called_names(body)
    assert spec.consults_transforms == ("apply_transforms" in calls)
    assert spec.consults_guards == ("guard_admits" in calls)
    assert spec.consults_transforms is True
    assert spec.consults_guards is False


def test_w1_skips_the_llm_reader_rather_than_refusing_it():
    """A name in NEITHER registry is a hard refusal (a real deploy error); a name in this
    one must be SKIPPED. Refusing would take the hourly W1 intake down for the whole portal
    the moment the bazos@3 contract loads — the remax@3 incident, again."""
    llm_only = entry("bzs.desc.street", block="description", field="street",
                     claim_type="street_name")
    result = claims_intake.extract_listing(listing_row(), [llm_only])
    assert result.claims == [] and result.absences == []


def test_an_unknown_reader_is_still_a_hard_refusal_in_w1():
    bogus = entry("bzs.desc.street", block="description", field="street",
                  claim_type="street_name", reader="llm_no_such_reader")
    with pytest.raises(IntakeRefused):
        claims_intake.extract_listing(listing_row(), [bogus])


# ------------------------------------------------------------------ the vocabulary

def test_every_field_maps_to_a_real_claim_type_and_nothing_was_invented():
    for field, claim_types in FIELD_CLAIM_TYPES.items():
        assert field in FIELD_ORDER, field
        for claim_type in claim_types:
            assert claim_type in contracts.CLAIM_TYPES, (field, claim_type)
            # The mirror is only as good as the enum it mirrors; read the DDL too.
            assert re.search(rf"^\s*'{claim_type}',?\s*(--.*)?$",
                             _MIGRATION_382 + _enum_ddl(), re.M), claim_type
    assert set(FIELD_ORDER) == set(FIELD_CLAIM_TYPES)
    assert set(LOCATION_TOOL["input_schema"]["properties"]) == {
        "from_title", "from_description"}
    for block in ("from_title", "from_description"):
        schema = LOCATION_TOOL["input_schema"]["properties"][block]
        assert set(schema["properties"]) == set(FIELD_CLAIM_TYPES)
        assert set(schema["required"]) == set(FIELD_CLAIM_TYPES)


def _enum_ddl() -> str:
    return (_ROOT / "migrations" / "380_location_w1_enums_and_config.sql").read_text("utf-8")


def test_the_prompt_is_pinned_so_an_unbumped_edit_reds():
    """`location_claim_fingerprint` (migration 386) hashes NEITHER `model` NOR
    `prompt_version` NOR any evidence field, so a prompt edit without a `PROMPT_VERSION`
    bump is PERMANENTLY silent: the re-run dedupes onto the old row and can never correct
    its attribution.

    If this fails because you deliberately changed the prompt or the tool schema: bump
    `PROMPT_VERSION` and this digest TOGETHER, in the same commit."""
    digest = hashlib.sha256(
        (SYSTEM_PROMPT + json.dumps(LOCATION_TOOL, sort_keys=True, ensure_ascii=False))
        .encode("utf-8")).hexdigest()
    assert digest == PINNED_PROMPT_DIGEST, (
        f"the prompt or the tool schema changed (digest {digest}); bump PROMPT_VERSION "
        f"and this literal together — the claim fingerprint hashes neither, so an "
        f"unbumped prompt edit is invisible forever")
    assert PROMPT_VERSION == "bzs.loc@1"


def test_the_prompt_never_carries_a_stored_column():
    """With stored columns in the prompt the design measured 11 high-confidence "stored
    echo" claims across 27 listings — the model reads back what we already believed and it
    looks like independent evidence."""
    message = build_user_message({"title": "T", "description": "D"})
    assert message == "TITULEK:\nT\n\nPOPIS:\nD\n"
    # The BODY, with the docstring dropped — the docstring names the stored columns
    # precisely in order to forbid them.
    body = _function("build_user_message").body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    source = "\n".join(ast.unparse(node) for node in body)
    for forbidden in ("listings.", "raw_json", "locality", "street", "psc"):
        assert forbidden not in source, forbidden


def test_the_reasoning_budget_is_the_house_convention():
    assert MAX_TOKENS == 4096


# ------------------------------------------------------------------ evidence completeness

def _evidence_columns_in_check(constraint: str) -> set[str]:
    body = re.search(rf"constraint\s+{constraint}\s+check\s*\((.*?)\)\);?",
                     _MIGRATION_382, re.S | re.I)
    assert body, f"{constraint} not found in migration 382"
    return set(re.findall(r"\b(\w+) is not null", body.group(1), re.I))


def test_a_claim_carries_every_column_the_db_check_demands():
    """The CHECK must never be the first line of defence: a batch is one transaction, so
    one malformed claim rolls back every good claim beside it."""
    required = _evidence_columns_in_check("loc_claim_text_evidence")
    assert required == {"evidence_quote", "span_start", "span_end",
                        "payload_scope_version", "subject_scoped"}
    llm_required = _evidence_columns_in_check("loc_claim_llm_model")
    assert llm_required == {"model", "prompt_version"}

    result = run_extract(street_entries(), answer(description={
        "street": stated("Sokolovská")}))
    assert len(result.claims) == 1
    claim = result.claims[0]
    for column in sorted(required | llm_required | {"payload_sha256", "payload_id"}):
        assert getattr(claim, column) is not None, column
    assert claim.span_end > claim.span_start
    assert claim.value_text == "Sokolovská"
    assert claim.extraction_method == "llm_text"
    assert claim.model == "gpt-5-nano"
    assert claim.prompt_version == PROMPT_VERSION
    assert claim.payload_sha256 == payload().payload_sha256
    assert claim.payload_scope_version == document().scope_version
    # The claim keeps the ENTRY's declared surface — C9 is a ruling about the DOM re-mine
    # lane, and `payload_*` already records that the text came from an archived body.
    assert claim.surface == "description"
    assert claim.snapshot_anchor == "unanchored_latest_fetch"
    assert claim.snapshot_id is None
    assert claim.to_row()["snapshot_id"] is None
    assert claim.first_observed_at == payload().first_observed_at
    assert claim.contract_entry_id == 7101
    assert claim.extractor_id == "bzs.desc.street"
    assert claim.extractor_version == "contract:bazos@3"


def test_the_span_indexes_into_the_scoped_html_and_quotes_the_value():
    doc = document()
    result = run_extract(street_entries(), answer(description={
        "street": stated("Sokolovská",
                         quote="na adrese Sokolovská 234, Praha 8 - Karlín")}))
    claim = result.claims[0]
    assert doc.html[claim.span_start:claim.span_end] == claim.evidence_quote


# ------------------------------------------------------------------ the hallucination guard

def test_a_quote_that_is_not_on_the_page_writes_no_claim_and_never_raises():
    """`assert_evidence_complete` would otherwise take a whole batch of paid work down on
    one bad response. This is a skip PLUS an absence, never a raise."""
    result = run_extract(street_entries(), answer(description={
        "street": stated("Sokolovská", quote="na Václavském náměstí 1")}))
    assert result.claims == []
    assert len(result.absences) == 1
    absence = result.absences[0]
    assert absence.reason in ("not_attempted", "only_in_excluded_block")
    assert "quote not locatable" in absence.detail
    assert absence.field_ == "street_name"


def test_a_value_only_in_an_excluded_zone_is_recorded_as_such():
    """"Peškova" is real text on the page — inside `.podobne`, which the register strips.
    The distinction between "the model made it up" and "the model read the neighbour
    carousel" is the whole reason the exclusion zones exist."""
    result = run_extract(street_entries(), answer(description={
        "street": stated("Peškova")}))
    assert result.claims == []
    assert result.absences[0].reason == "only_in_excluded_block"


def test_a_broken_scoper_writes_one_absence_per_entry_and_extracts_nothing():
    """`html_scope` fails CLOSED: an incomplete result ADMITS NOTHING. "The scoper broke"
    must never read as "no zones matched, extract freely"."""
    broken = ScopeRegister.from_zones("bazos", (
        {"locator_kind": "html_selector", "locator": {"css": "div::totally-bogus"}},))
    doc = scope_html(FIXTURE, register=broken)
    assert not doc.is_complete
    entries = street_entries()
    result = extract_payload(
        payload(), listing_row(), entries,
        answer(description={"street": stated("Sokolovská")}),
        document=doc, model="gpt-5-nano", gazetteer=FakeGazetteer())
    assert result.claims == []
    assert len(result.absences) == len(entries)
    assert {a.reason for a in result.absences} == {"not_attempted"}
    assert all("scoping incomplete" in a.detail for a in result.absences)


# ------------------------------------------------------------------ description beats title

def test_the_description_wins_and_only_one_claim_is_written():
    """The operator's ruling, expressed where it is expressible: ONE claim per field,
    description-first. `location_field_policy` matches on (source, extraction_method) only,
    so two llm_text claims from one portal cannot be ranked as data at all."""
    result = run_extract(street_entries(), answer(
        description={"street": stated("Sokolovská")},
        title={"street": stated("Sokolovská")}))
    assert len(result.claims) == 1
    assert result.claims[0].extractor_id == "bzs.desc.street"
    assert result.claims[0].surface == "description"
    assert result.absences == []


def test_the_title_is_the_fallback_when_the_description_says_nothing():
    result = run_extract(street_entries(), answer(
        title={"street": stated("Sokolovská")}))
    assert len(result.claims) == 1
    assert result.claims[0].extractor_id == "bzs.title.street"
    assert result.claims[0].surface == "html_selector"


def test_a_description_the_gazetteer_refuses_falls_through_to_the_title():
    """The rung is "the description STATED it usably", not "the description was silent" —
    a refused description value must not silently suppress a good headline one."""
    gazetteer = FakeGazetteer(streets={554782: {"sokolovska"}})
    result = run_extract(
        street_entries(),
        answer(description={"street": stated("Nový")},
               title={"street": stated("Sokolovská")}),
        gazetteer=gazetteer)
    assert len(result.claims) == 1
    assert result.claims[0].extractor_id == "bzs.title.street"
    assert result.absences == []


def test_a_contract_declaring_two_entries_for_one_block_and_field_is_refused():
    duplicate = [
        entry("bzs.desc.street", block="description", field="street",
              claim_type="street_name", entry_pk=1),
        entry("bzs.desc.street2", block="description", field="street",
              claim_type="street_name", entry_pk=2),
    ]
    with pytest.raises(IntakeRefused, match="one field per block"):
        run_extract(duplicate, answer(description={"street": stated("Sokolovská")}))


def test_one_block_declared_with_two_selectors_is_refused():
    conflicting = [
        entry("bzs.desc.street", block="description", field="street",
              claim_type="street_name", entry_pk=1),
        entry("bzs.desc.obec", block="description", field="obec",
              claim_type="obec_name", css="div.other", entry_pk=2),
    ]
    with pytest.raises(IntakeRefused, match="two different"):
        run_extract(conflicting, answer())


def test_a_field_mapped_to_the_wrong_claim_type_is_refused():
    wrong = [entry("bzs.desc.street", block="description", field="street",
                   claim_type="obec_name")]
    with pytest.raises(IntakeRefused, match="may only claim"):
        run_extract(wrong, answer())


# ------------------------------------------------------------------ the gazetteer gate

@pytest.mark.parametrize("field,claim_type,value", [
    ("obec", "obec_name", "Vymyšlenice"),
    ("cast_obce", "cast_obce_name", "Vymyšlenice"),
])
def test_a_name_the_registry_does_not_carry_writes_no_claim(field, claim_type, value):
    entries = [entry(f"bzs.desc.{field}", block="description", field=field,
                     claim_type=claim_type)]
    result = run_extract(entries, answer(description={
        field: stated(value, quote="Prodáme byt 3+1")}))
    assert result.claims == []
    assert len(result.absences) == 1
    assert result.absences[0].reason == "stated_but_ambiguous"
    assert "gazetteer" in result.absences[0].detail


def test_a_street_the_candidate_obec_does_not_carry_writes_no_claim():
    """The phantom-street guard regression 220870847 asked for: a REGISTRY membership
    test, not the morphology heuristic `bzs.det.street_text` declared and never built."""
    entries = [
        entry("bzs.desc.obec", block="description", field="obec",
              claim_type="obec_name", entry_pk=1),
        entry("bzs.desc.street", block="description", field="street",
              claim_type="street_name", entry_pk=2),
    ]
    result = run_extract(entries, answer(description={
        "obec": stated("Praha 8"),
        "street": stated("Nový", quote="Prodáme byt 3+1")}))
    assert [c.claim_type for c in result.claims] == ["obec_name"]
    refusal = [a for a in result.absences if a.field_ == "street_name"]
    assert len(refusal) == 1
    assert refusal[0].reason == "stated_but_ambiguous"
    assert "no street" in refusal[0].detail


def test_a_street_passes_when_no_candidate_obec_could_be_resolved():
    """Mirrors S7 exactly (`core._gazetteer_validate` passes when obec_kod is None): no
    constraint is not a rejection, and the lane must not drop a claim the resolver would
    have accepted."""
    assert gazetteer_refusal("street_name", "Nový", gazetteer=FakeGazetteer(),
                             obec_kod=None, street_norm=None) is None


def test_a_house_number_is_refused_outright_when_no_obec_is_known():
    """The opposite call from the street's, and deliberately: there is no honest check to
    make, and doctrine #5 forbids a nearest-neighbour snap."""
    refusal = gazetteer_refusal("house_number_cp", "234", gazetteer=FakeGazetteer(),
                                obec_kod=None, street_norm=None)
    assert isinstance(refusal, Refusal)
    assert refusal.reason == "stated_but_ambiguous"
    assert "no candidate obec" in refusal.detail


def test_a_non_integer_house_number_is_refused():
    refusal = gazetteer_refusal("house_number_cp", "234a", gazetteer=FakeGazetteer(),
                                obec_kod=554782, street_norm="sokolovska")
    assert isinstance(refusal, Refusal)
    assert "not an integer" in refusal.detail


def test_the_house_number_is_checked_against_the_street_this_listing_claimed():
    entries = [
        entry("bzs.desc.obec", block="description", field="obec",
              claim_type="obec_name", entry_pk=1),
        entry("bzs.desc.street", block="description", field="street",
              claim_type="street_name", entry_pk=2),
        entry("bzs.desc.cp", block="description", field="house_number",
              claim_type="house_number_cp", transform=("split_cp_co:cp",), entry_pk=3),
    ]
    result = run_extract(entries, answer(description={
        "obec": stated("Praha 8"),
        "street": stated("Sokolovská"),
        "house_number": stated("234", quote="Sokolovská 234")}))
    assert {c.claim_type for c in result.claims} == {
        "obec_name", "street_name", "house_number_cp"}
    assert result.absences == []


def test_the_landmark_and_the_verbatim_line_are_not_gazetteer_gated():
    """Both are permanently `is_admin_bearing = FALSE`; there is nothing to reconcile them
    against, and storing them un-gated is the honest treatment rather than a gap."""
    for claim_type in ("landmark", "address_line_verbatim", "psc"):
        assert gazetteer_refusal(claim_type, "u kostela", gazetteer=FakeGazetteer(),
                                 obec_kod=None, street_norm=None) is None


def test_the_candidate_obec_ladder_is_description_then_title_then_psc():
    gazetteer = FakeGazetteer()
    assert _candidate_obec(answer(description={"obec": stated("Praha 8")}),
                           gazetteer=gazetteer, psc=None) == (554782, "obec_from_llm_description")
    assert _candidate_obec(answer(title={"obec": stated("Praha 8")}),
                           gazetteer=gazetteer, psc=None) == (554782, "obec_from_llm_title")
    assert _candidate_obec(answer(), gazetteer=gazetteer, psc="186 00") == (
        554782, "obec_from_psc")
    assert _candidate_obec(answer(), gazetteer=gazetteer, psc=None) == (
        None, "obec_unknown")


def test_an_ambiguous_obec_name_is_not_a_candidate():
    """Two codes for one name is a homonym, and a homonym is not a constraint."""
    gazetteer = FakeGazetteer(obec_codes={"praha 8": [1, 2]})
    assert _candidate_obec(answer(description={"obec": stated("Praha 8")}),
                           gazetteer=gazetteer, psc=None) == (None, "obec_unknown")


# ------------------------------------------------------------------ the model's answer

def test_a_low_confidence_answer_is_recorded_but_never_claimed():
    result = run_extract(street_entries(), answer(description={
        "street": stated("Sokolovská", confidence="low")}))
    assert result.claims == []
    assert result.absences[0].reason == "stated_but_ambiguous"
    assert "confidence" in result.absences[0].detail


@pytest.mark.parametrize("model_confidence,expected", [("high", "high"),
                                                       ("medium", "medium")])
def test_the_claim_carries_the_models_own_confidence(model_confidence, expected):
    result = run_extract(street_entries(), answer(description={
        "street": stated("Sokolovská", confidence=model_confidence)}))
    assert result.claims[0].claim_confidence == expected


def test_a_malformed_field_is_an_absence_and_never_an_exception():
    """`additionalProperties: false` reaches OpenAI/Qwen as an ordinary schema key with no
    `strict: true`, so it is advisory — one bad shape must not roll back a paid batch."""
    broken = {"from_description": {"street": "Sokolovská"}, "from_title": {}}
    result = run_extract(street_entries(), broken)
    assert result.claims == []
    assert len(result.absences) == 1
    assert result.absences[0].reason == "not_attempted"

    assert isinstance(parse_field_answer({}, "description", "street"), Refusal)
    assert isinstance(parse_field_answer(
        {"from_description": {"street": {"value": "x", "quote": "x",
                                         "confidence": "very sure"}}},
        "description", "street"), Refusal)


def test_an_empty_answer_produces_one_absence_per_field_and_no_claim():
    """A model that answered without calling the forced tool has said nothing. Six
    fourteen-entry blocks collapse to one absence per OUTPUT FIELD, never per entry."""
    entries = full_entries()
    result = run_extract(entries, {})
    assert result.claims == []
    assert len(result.absences) == len({(e.claim_type, e.transform) for e in entries})


# ------------------------------------------------------------------ transforms

def test_one_stated_number_serves_the_cp_entry_and_the_co_entry():
    entries = [
        entry("bzs.desc.obec", block="description", field="obec",
              claim_type="obec_name", entry_pk=1),
        entry("bzs.desc.cp", block="description", field="house_number",
              claim_type="house_number_cp", transform=("split_cp_co:cp",), entry_pk=2),
        entry("bzs.desc.co", block="description", field="house_number",
              claim_type="house_number_co", transform=("split_cp_co:co",), entry_pk=3),
    ]
    gazetteer = FakeGazetteer(points={(554782, None, 1216, None),
                                      (554782, None, None, 46)})
    # A body whose DESCRIPTION states a čp/čo pair. The shipped fixture's only such pair is
    # in the footer, which the register strips — quoting it would (correctly) be refused as
    # `only_in_excluded_block`, which is a different test.
    body = (b"<html><body>"
            b"<h1 class='nadpisdetail'>Prodej bytu 3+1, Praha 8</h1>"
            b"<div class='popisdetail'>Prod\xc3\xa1me byt na adrese "
            b"Sokolovsk\xc3\xa1 1216/46, Praha 8 - Karl\xc3\xadn.</div>"
            b"</body></html>")
    result = run_extract(entries, answer(description={
        "obec": stated("Praha 8"),
        "house_number": stated("1216/46", quote="Sokolovská 1216/46")}),
        gazetteer=gazetteer, document=document(body))
    by_type = {c.claim_type: c.value_text for c in result.claims}
    assert by_type == {"obec_name": "Praha 8", "house_number_cp": "1216",
                       "house_number_co": "46"}


def test_a_bare_number_yields_nothing_for_the_co_entry_by_construction():
    """`split_cp_co:co` returns None without a slash — a measured limitation, recorded as
    an absence rather than guessed at."""
    entries = [
        entry("bzs.desc.obec", block="description", field="obec",
              claim_type="obec_name", entry_pk=1),
        entry("bzs.desc.co", block="description", field="house_number",
              claim_type="house_number_co", transform=("split_cp_co:co",), entry_pk=2),
    ]
    result = run_extract(entries, answer(description={
        "obec": stated("Praha 8"),
        "house_number": stated("234", quote="Sokolovská 234")}))
    assert [c.claim_type for c in result.claims] == ["obec_name"]
    co = [a for a in result.absences if a.field_ == "house_number_co"]
    assert len(co) == 1
    assert co[0].reason == "not_stated"
    assert "transform" in co[0].detail


# ------------------------------------------------------------------ write bounds

def test_claims_are_appended_per_listing_so_a_chunk_cannot_split_one():
    """`write_result` chunks by row count and bytes and never splits a listing, which is
    only true because the lane appends a whole listing's claims contiguously."""
    entries = full_entries()
    combined = []
    for listing_id in (5150, 5151):
        result = extract_payload(
            payload(), listing_row(listing_id=listing_id), entries,
            answer(description={"obec": stated("Praha 8"),
                                "street": stated("Sokolovská")}),
            document=document(), model="gpt-5-nano", gazetteer=FakeGazetteer())
        combined.extend(result.claims)
    seen: list[int] = []
    for claim in combined:
        if not seen or seen[-1] != claim.listing_id:
            seen.append(claim.listing_id)
    assert seen == sorted(set(seen)), "a listing's claims must be contiguous"


def test_an_oversized_value_becomes_an_absence_and_no_refetch_row():
    """An archived body is immutable and content-addressed, so a refetch would be a
    permanently failing attempt counter. The fix is a narrower prompt or a transform."""
    result = run_extract(street_entries(),
                         answer(description={"street": stated("Sokolovská")}),
                         max_value_bytes=4)
    assert result.claims == []
    assert result.oversized == 1
    assert result.enrichment == []
    assert result.absences[0].reason == "not_attempted"


# ------------------------------------------------------------------ dedupe / fingerprint

def test_the_same_payload_and_answer_produce_a_byte_identical_claim():
    """The lane is a pure function of (payload, entries, answer, document): re-running it
    over an unchanged payload produces the same rows, which is what makes
    `location_claim_fingerprint`'s unique index a no-op rather than a duplicate insert."""
    first = run_extract(street_entries(), answer(description={
        "street": stated("Sokolovská")}))
    second = run_extract(street_entries(), answer(description={
        "street": stated("Sokolovská")}))
    assert [c.to_row() for c in first.claims] == [c.to_row() for c in second.claims]
    # Every column the fingerprint hashes is identical, INCLUDING the ones it does not
    # (model / prompt_version) — those are exactly why the prompt digest above is pinned.
    fingerprinted = ("listing_id", "claim_type", "surface", "page_kind",
                     "extraction_method", "extractor_id", "extractor_version",
                     "contract_entry_id", "value_text")
    for column in fingerprinted:
        assert getattr(first.claims[0], column) == getattr(second.claims[0], column)


def test_an_answer_for_a_different_page_kind_is_not_executed():
    index_entry = entry("bzs.idx.street", block="description", field="street",
                        claim_type="street_name", page_kind="index")
    assert llm_entries([index_entry], "detail") == []
    result = run_extract([index_entry], answer(description={
        "street": stated("Sokolovská")}))
    assert result.claims == [] and result.absences == []


# ------------------------------------------------------------------ structural rails

def _function(name: str) -> ast.FunctionDef:
    for node in ast.walk(_LANE_AST):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in claims_llm.py")


def _reader_body(name: str) -> ast.FunctionDef:
    for node in _LANE_AST.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if (isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Name)
                    and decorator.func.id == "llm_reader"
                    and decorator.args
                    and isinstance(decorator.args[0], ast.Constant)
                    and decorator.args[0].value == name):
                return node
    raise AssertionError(f"no @llm_reader({name!r}) in claims_llm.py")


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_the_model_is_never_called_inside_an_open_transaction():
    """THE transaction rule. A single call takes seconds, and holding a `guarded()`
    transaction across a batch of them is idle-in-transaction on the transaction-mode
    pooler for the whole batch."""
    offenders: list[int] = []
    for node in ast.walk(_LANE_AST):
        if not isinstance(node, ast.With):
            continue
        opens_txn = any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Name)
            and item.context_expr.func.id == "guarded"
            for item in node.items)
        if not opens_txn:
            continue
        for child in ast.walk(node):
            if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                    and child.func.attr in ("answer", "call", "complete")):
                offenders.append(child.lineno)
    assert not offenders, (
        f"a model call appears inside a `with guarded(...)` block at line(s) {offenders}; "
        f"that is idle-in-transaction on the pooler for the whole batch")


def test_the_bakeoff_writes_no_claims_and_declares_no_lane():
    """It is a MEASUREMENT harness. It must not be able to write a claim by accident, and
    `tests/location_data/test_lane_identifiers.py` scans `scripts/location_*.py` — so a
    LANE / JOB_NAME / version constant here would enter the fleet's identity namespaces."""
    text = _BAKEOFF_PATH.read_text("utf-8")
    for forbidden in ("write_result", "_CLAIM_WRITE_SQL", "INSERT INTO location_claims",
                      "insert into location_claims", "claims_llm.run"):
        assert forbidden not in text, forbidden
    module_constants = {
        target.id
        for node in _BAKEOFF_AST.body if isinstance(node, ast.Assign)
        for target in node.targets if isinstance(target, ast.Name)
    }
    assert not module_constants & {"LANE", "JOB_NAME", "CONCURRENCY_GROUP", "LLM_VERSION",
                                   "INTAKE_VERSION", "REMINE_VERSION"}
    assert "location_llm_bakeoff" in text


def test_every_bazos_llm_entry_is_executable():
    """bazos@3 is the bump that names `llm_location_text`, so this reads the SHIPPED
    contract off disk rather than the hand-built `full_entries()` above.

    "Executable" is four things at once, and each has its own failure mode: the reader
    resolves in `LLM_READERS` (a name resolving in the wrong registry reads the wrong
    substrate); the entry is declared for the page kind the lane actually scans; the
    contract's own `(block, field, claim_type, transform)` quadruple is one
    `_validated_groups` accepts (a contradiction here refuses the run AFTER the batch is
    open, so it must red in CI instead); and the on-disk set is exactly the plan the rest of
    this module drives, so the hermetic fixtures cannot drift away from what production
    executes.
    """
    entries = entries_for("bazos")
    llm = llm_entries(entries, claims_llm.PAGE_KIND)
    assert llm, "bazos@3 declares no executable LLM entry — the lane is inert again"

    shipped = {
        (str(e.locator["llm_block"]), str(e.locator["llm_field"]), e.claim_type,
         e.transform, str(e.locator["css"]))
        for e in llm
    }
    planned = {
        (str(e.locator["llm_block"]), str(e.locator["llm_field"]), e.claim_type,
         e.transform, str(e.locator["css"]))
        for e in full_entries()
    }
    assert shipped == planned, (
        "the shipped bazos LLM entries and this module's hand-built set have diverged; "
        "every hermetic assertion below drives the hand-built one, so a difference here "
        "means production runs something nothing in this file tests")

    for entry_ in llm:
        assert entry_.reader in LLM_READERS
        assert entry_.extraction_method == "llm_text"
        assert entry_.page_kind == claims_llm.PAGE_KIND
        assert entry_.claim_type in FIELD_CLAIM_TYPES[str(entry_.locator["llm_field"])]

    css_by_block, groups = _validated_groups(llm)
    assert set(css_by_block) == set(BLOCK_ORDER)
    # Every output field carries BOTH rungs: a block the contract does not declare is a
    # block the lane never reads, so an undeclared title rung would silently discard the
    # headline answer the model was paid to produce.
    for key, rungs in groups.items():
        assert list(rungs) == list(BLOCK_ORDER), key


def test_no_other_portal_contract_names_an_llm_reader():
    """The lane is bazos-only by design (`DEFAULT_SOURCE`), and a second portal naming
    `llm_location_text` needs its own `location_field_policy` rung or its claims are stored
    and declined at S7 forever — which looks exactly like a lane that is not running."""
    named = {contract.source: [e.entry_id for e in contract.entries
                               if e.reader in LLM_READERS]
             for contract in contracts.load_all()}
    assert {s: ids for s, ids in named.items() if ids and s != "bazos"} == {}


def test_a_field_policy_rung_exists_for_every_survivorship_field_bazos_llm_emits():
    """Migration 472, read off disk. A claim whose (source, extraction_method) matches no
    `location_field_policy` row is not "unranked" — `_best_policy` returns None, the claim
    is SKIPPED, and the field lands in `survivorship_blocked` forever. So the contract bump
    and its policy rows are one change, and this is the test that says so.

    The intersection is deliberate: `landmark` and `address_line_verbatim` are real claim
    types that no field is ever won from, so they must NOT have rows (dead config), and the
    six that remain must.
    """
    from location_data.resolver.core import SURVIVORSHIP_FIELDS

    sql = (_ROOT / "migrations" / "472_location_bazos_llm_field_policy.sql").read_text(
        "utf-8")
    body = sql.split("begin;", 1)[1]
    assert "'portal:bazos', 'llm_text', 350" in body
    for flag in ("'medium'::match_confidence", "true, false, false"):
        assert flag in body, flag

    emitted = {e.claim_type for e in llm_entries(entries_for("bazos"),
                                                 claims_llm.PAGE_KIND)}
    arbitrated = emitted & set(SURVIVORSHIP_FIELDS)
    assert arbitrated == {"obec_name", "cast_obce_name", "psc", "street_name",
                          "house_number_cp", "house_number_co"}
    seeded = set(re.findall(r"'([a-z_]+)'", body.split("unnest(array[", 1)[1]
                            .split("]::location_claim_type[]", 1)[0]))
    assert seeded == arbitrated, (
        f"migration 472 seeds {sorted(seeded)} but bazos@3 emits {sorted(arbitrated)} "
        f"survivorship fields through llm_text")
    assert not (emitted - set(SURVIVORSHIP_FIELDS)) & seeded


def test_the_estimate_used_by_the_preflight_cap_is_never_zero():
    """A pre-flight cap that estimates $0 is not a cap. An unknown model falls back to a
    conservative per-call figure rather than to nothing."""
    assert estimated_cost_usd("gpt-5-nano", 150) > 0
    assert estimated_cost_usd("a-model-nobody-priced", 150) > 0


def test_block_texts_are_truncated_and_read_only_the_declared_nodes():
    doc = document()
    texts = block_texts(doc, {"description": DESCRIPTION_CSS, "title": TITLE_CSS})
    assert "Sokolovská 234" in texts["description"]
    assert "Prodej bytu 3+1" in texts["title"]
    # The neighbour carousel is stripped before any selector runs.
    assert "Peškova" not in texts["description"] + texts["title"]
    assert all(len(v) <= claims_llm.MAX_BLOCK_CHARS for v in texts.values())
    missing = block_texts(doc, {"description": "div.no-such-node"})
    assert missing == {"description": ""}


def test_validated_groups_orders_the_ladder_description_first():
    css_by_block, groups = _validated_groups(street_entries())
    assert css_by_block == {"description": DESCRIPTION_CSS, "title": TITLE_CSS}
    assert list(groups) == [("street_name", ())]
    assert list(groups[("street_name", ())]) == ["description", "title"]
    assert BLOCK_ORDER == ("description", "title")


def test_resolve_model_prefers_the_cli_override_then_app_settings_then_the_default():
    class FakeCursor:
        def __init__(self, value): self._value = value
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): return None
        def fetchone(self): return None if self._value is None else (self._value,)

    class FakeConn:
        def __init__(self, value): self._value = value
        def cursor(self): return FakeCursor(self._value)

    assert resolve_model(FakeConn("gpt-5.6-luna"), "qwen3.7-flash") == "qwen3.7-flash"
    assert resolve_model(FakeConn("gpt-5.6-luna"), None) == "gpt-5.6-luna"
    assert resolve_model(FakeConn(None), None) == claims_llm.DEFAULT_MODEL


# The digest of SYSTEM_PROMPT + LOCATION_TOOL. Bump it and PROMPT_VERSION TOGETHER.
PINNED_PROMPT_DIGEST = "8a51544867ea09122ea956170d6c788abadddc32f609f65191393c57587eeba9"


# ------------------------------------------------------------------ the run, hermetically

class _FakeCursor:
    """Just enough cursor to answer this lane's five statements from canned data."""

    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []
        self._one: tuple[Any, ...] | None = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        conn = self._conn
        conn.statements.append(sql)
        params = params or {}
        if sql is claims_intake._TIMEOUT_GUARD_SQL:
            return
        if sql is claims_llm._ACTIVE_CONTRACT_SQL:
            self._one = (3, 3)
        elif sql is claims_llm._RESUME_SQL:
            self._one = None
        elif sql is claims_llm._WATERMARK_SQL:
            self._one = (None,)
        elif sql is claims_llm._BATCH_INSERT_SQL:
            conn.batches.append(dict(params))
            self._one = (77, None)
        elif sql is claims_llm._BATCH_FINISH_SQL:
            conn.finished.append(dict(params))
        elif sql is claims_llm._LLM_SCAN_SQL:
            conn.scans.append(dict(params))
            after_id = int(params["after_id"])
            pending = [r for r in conn.payload_rows if r[0] > after_id]
            self._rows = pending[:int(params["batch_size"])]
        elif "portal_raw_payloads" in sql and "body_r2_key" in sql:
            self._rows = [(pid, conn.bodies[pid], None, "identity")
                          for pid in params["ids"]]
        elif "location_claims" in sql and "insert" in sql.lower():
            rows = params["rows"].obj if hasattr(params["rows"], "obj") else params["rows"]
            conn.written_claims.extend(rows)
            self._one = (len(rows), len(rows), len(rows))
        elif "location_claim_absences" in sql:
            rows = params["rows"].obj if hasattr(params["rows"], "obj") else params["rows"]
            conn.written_absences.extend(rows)
        elif "location_enrichment_state" in sql:
            pass
        else:  # pragma: no cover - a statement this fake does not model
            raise AssertionError(f"unmodelled statement: {sql[:80]}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._one

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _FakeTxn:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: Any) -> bool:
        return False


class _FakeConn:
    def __init__(self, count: int = 2) -> None:
        self.statements: list[str] = []
        self.batches: list[dict[str, Any]] = []
        self.finished: list[dict[str, Any]] = []
        self.scans: list[dict[str, Any]] = []
        self.written_claims: list[dict[str, Any]] = []
        self.written_absences: list[dict[str, Any]] = []
        self.payload_rows = [
            (9100 + i, "bazos", f"22002{i}", "detail", "cd" * 32, FETCHED_AT,
             5150 + i, False, "186 00")
            for i in range(count)
        ]
        self.bodies = {9100 + i: FIXTURE for i in range(count)}

    def transaction(self) -> _FakeTxn:
        return _FakeTxn()

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)


class FakeCaller:
    """A canned tool answer, the `--fake-llm` shape. No network, no key, no spend."""

    def __init__(self, model_answer: dict[str, Any], cost: float = 0.0004) -> None:
        self._answer = model_answer
        self.cost = cost
        self.calls = 0
        self.blocks_seen: list[dict[str, str]] = []

    def answer(self, blocks: dict[str, str]) -> tuple[dict[str, Any], float]:
        self.calls += 1
        self.blocks_seen.append(dict(blocks))
        return dict(self._answer), self.cost


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(claims_llm, "missing_relations", lambda conn: [])
    monkeypatch.setattr(claims_llm, "assert_inventory_ready", lambda conn: 2201)
    monkeypatch.setattr(claims_llm, "load_register", lambda conn, source: REGISTER)
    return monkeypatch


def _run(conn, monkeypatch, entries: list[Entry], caller, **kwargs: Any):
    monkeypatch.setattr(claims_llm, "load_entries", lambda c: {"bazos": entries})
    defaults: dict[str, Any] = {
        "mode": "full", "source": "bazos", "batch_size": 10, "statement_timeout": 30,
        "model": "gpt-5-nano", "llm": caller, "gazetteer": FakeGazetteer(),
        "store": object(),
    }
    defaults.update(kwargs)
    return claims_llm.run(conn, **defaults)


def test_a_run_with_no_executable_entry_opens_no_batch_and_calls_no_model(wired):
    """Inert must not mean 'ok'. A batch that reached the end of its scan is stamped 'ok',
    and 'ok' is what the incremental watermark reads."""
    conn = _FakeConn()
    inert_entry = entry("bzs.det.locality_text", block="description", field="street",
                        claim_type="street_name", reader="legacy_text_column")
    caller = FakeCaller(answer())
    stats = _run(conn, wired, [inert_entry], caller)
    assert stats["outcome"] == "inert"
    assert conn.batches == [] and conn.scans == []
    assert caller.calls == 0


def test_a_full_pass_calls_once_per_listing_and_writes_one_claim_per_field(wired):
    conn = _FakeConn(count=2)
    caller = FakeCaller(answer(description={
        "obec": stated("Praha 8"),
        "street": stated("Sokolovská", quote="Sokolovská 234")}))
    stats = _run(conn, wired, full_entries(), caller)
    assert stats["outcome"] == "ok"
    assert stats["payloads"] == 2
    assert caller.calls == 2
    assert stats["calls"] == 2
    assert stats["claims"] == 4  # obec + street, per listing
    assert stats["claims_inserted"] == 4
    assert stats["spent_usd"] == pytest.approx(0.0008)
    assert len(conn.batches) == 1
    assert conn.batches[0]["lane"] == claims_llm.LANE
    assert conn.batches[0]["extractor_version"] == claims_llm.LLM_VERSION
    assert conn.finished[-1]["outcome"] == "ok"
    assert {row["claim_type"] for row in conn.written_claims} == {
        "obec_name", "street_name"}
    assert all(row["model"] == "gpt-5-nano" for row in conn.written_claims)
    assert all(row["prompt_version"] == PROMPT_VERSION for row in conn.written_claims)


def test_the_prompt_the_lane_builds_carries_only_the_scoped_blocks(wired):
    conn = _FakeConn(count=1)
    caller = FakeCaller(answer())
    _run(conn, wired, full_entries(), caller)
    blocks = caller.blocks_seen[0]
    assert set(blocks) == {"description", "title"}
    assert "Sokolovská 234" in blocks["description"]
    # The neighbour carousel and the footer are stripped before any selector runs, so a
    # street from either can never reach the model in the first place.
    assert "Peškova" not in blocks["description"] + blocks["title"]
    assert "Klimentská" not in blocks["description"] + blocks["title"]


def test_the_scan_is_keyed_on_the_model_and_prompt_that_would_be_billed(wired):
    """The spend guard: a payload THIS model and prompt already produced a claim from is
    never re-called."""
    conn = _FakeConn(count=1)
    _run(conn, wired, full_entries(), FakeCaller(answer()))
    assert conn.scans[0]["model"] == "gpt-5-nano"
    assert conn.scans[0]["prompt_version"] == PROMPT_VERSION
    assert conn.scans[0]["source"] == "bazos"


def test_a_dry_run_calls_nothing_writes_nothing_and_opens_no_batch(wired):
    conn = _FakeConn(count=2)
    caller = FakeCaller(answer(description={"street": stated("Sokolovská")}))
    stats = _run(conn, wired, full_entries(), caller, dry_run=True)
    assert caller.calls == 0
    assert conn.batches == []
    assert conn.written_claims == []
    assert stats["claims_inserted"] == 0


def test_a_provider_outage_stamps_the_batch_failed_rather_than_reporting_success(wired):
    class Outage:
        def answer(self, blocks):
            raise RuntimeError("no credits remaining")

    conn = _FakeConn(count=10)
    with pytest.raises(IntakeRefused, match="consecutive model failures"):
        _run(conn, wired, full_entries(), Outage())
    assert conn.finished[-1]["outcome"] == "failed"


def test_the_preflight_cap_refuses_before_a_single_call_is_billed(wired):
    conn = _FakeConn(count=2)
    caller = FakeCaller(answer())
    with pytest.raises(IntakeRefused, match="pre-flight estimate"):
        _run(conn, wired, full_entries(), caller, limit=100000, max_usd=0.01)
    assert caller.calls == 0
    assert conn.batches == []


def test_a_budget_stopped_run_is_stamped_stopped_so_the_watermark_stays_put(wired):
    conn = _FakeConn(count=5)
    caller = FakeCaller(answer(description={"street": stated("Sokolovská")}))
    stats = _run(conn, wired, full_entries(), caller, limit=2, batch_size=2)
    assert stats["outcome"] == "stopped"
    assert stats["payloads"] == 2
    assert conn.finished[-1]["outcome"] == "stopped"


def test_a_mid_batch_budget_stop_advances_the_cursor_only_over_rows_it_looked_at(wired):
    """A batch is a BILL. Stopping mid-batch must leave the rest of that batch in the next
    run's window — advancing the cursor to the batch's last row would skip un-called
    listings permanently, and the batch is stamped 'stopped', which the watermark cannot
    see."""
    conn = _FakeConn(count=3)
    caller = FakeCaller(answer(description={"street": stated("Sokolovská")}))
    stats = _run(conn, wired, full_entries(), caller, limit=1, batch_size=3)
    assert stats["outcome"] == "stopped"
    assert caller.calls == 1
    assert stats["payloads"] == 1
    # The FIRST scanned payload, not the third.
    assert stats["cursor_after_id"] == conn.payload_rows[0][0]
    assert conn.finished[-1]["outcome"] == "stopped"
