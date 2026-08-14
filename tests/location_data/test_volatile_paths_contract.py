"""The volatile profiles live in the portal contracts, and a bad one fails LOUDLY.

W2a-3b. `location_data.payload_norm.MEASURED_VOLATILE_PROFILES` is retired: each
portal's rules are `persistence.volatile_paths.<page_kind>` in
`contracts/portals/<portal>.yaml`, so a change to what a portal strips is a reviewed,
versioned, retractable diff instead of a Python edit.

Three properties, in the order the risk runs:

  1. THE MOVE CHANGED NOTHING. `payload_sha256` is a permanent content address, so a
     byte that moves here re-addresses the archive and makes every measurement taken
     so far incomparable. Pinned two independent ways: as digests of the resolved
     profiles, computed under the RETIRED table's values, and (in
     test_payload_norm_by_page_kind) as digests of the normalised bytes of all 26
     committed detail fixtures.
  2. A BAD SELECTOR CANNOT REACH `normalise`. It is silent by contract — `.css()`
     raises on a typo and `:contains()` SEGFAULTS the parser (exit 139, uncatchable)
     — so a mistake that gets that far does not fail, it quietly STOPS STRIPPING and
     the portal's change rate moves for a reason nobody can see. Both loaders refuse
     it instead, and every contract on disk is parsed by this suite.
  3. THE LABEL CANNOT LIE. `normalizer_version` explains a permanent content address,
     so it names the engine AND the contract version that supplied the profile, and
     it is resolved as one value with the profile it names.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from location_data import contracts
from location_data.payload_norm import (
    BASE_PROFILE,
    BASE_PROFILE_SUFFIX,
    CONTRACT_DIR,
    CONTRACT_PROFILE_SUFFIX,
    NORMALIZER_VERSION,
    PAGE_KIND_DETAIL,
    ProfileError,
    VolatileProfile,
    contract_profiles,
    load_contract_profiles,
    parse_volatile_paths,
    resolve_normalisation,
    selector_is_usable,
    volatile_profile,
)

_FLEET = frozenset({
    "sreality", "bezrealitky", "bazos", "idnes", "mmreality",
    "remax", "ceskereality", "realitymix", "maxima",
})


def _profile_digest(profile: VolatileProfile) -> str:
    blob = json.dumps(
        [list(profile.json_pointers), list(profile.css_selectors),
         list(profile.strip_attributes)],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# Computed from `MEASURED_VOLATILE_PROFILES` as it stood on origin/main BEFORE the
# relocation — i.e. from the code the contracts replace. Regenerating one of these to
# make the suite pass is never the fix: it would silently move a permanent content
# address and make every churn number measured so far incomparable. A deliberate
# profile change re-measures, bumps the portal's `contract_version` (which opens a
# clean cohort by itself) and updates the digest in the same reviewed diff.
_RETIRED_TABLE_DIGESTS: dict[str, str] = {
    "bazos": "1bde1c8579b5c7beaf7deaf3565d7403ccafdc39c47892b85b424746787d47cc",
    "bezrealitky": "5ae1625b488b3935122d8dd627fe575b388a5aa360378fa4407aad08baaed1e2",
    "ceskereality": "25c7898bea69761bf9abf8f6b39c60f17a5c1c1fda3c668b85ce5829661b9a8d",
    "idnes": "4574b9ef8117cc09bbc51cf97b03883e6632ce91cda3d188b0cb8c4735fd3e6f",
    "maxima": "3ca5c2f91c6bb4c3ea0287d9bca8b5fceb5e947162535bf93c7662cec6594f35",
    "mmreality": "2758ed02f2d42061705adbd721688f3cd3d735d43c2e05f44e3e9eee5bd54ca6",
    "realitymix": "c3c5f1647883fe6a8dd2d644af25f7b8bebfd03d1dbcf3ad51dfacfc8d826b3e",
    "remax": "565238ef39b389c2cd11307b4d819d0169c3ce75fc6f718f13b9c66cc3c1cad1",
    "sreality": "009d8bcc4fb1690ccc816b41682e24a2d88b7c3df9497cb9e146502adfa89c61",
}


# ------------------------------------------------------ 1. the move changed nothing

@pytest.mark.parametrize("source", sorted(_RETIRED_TABLE_DIGESTS))
def test_the_contract_resolves_the_profile_the_python_table_used_to_hold(
    source: str,
) -> None:
    """A RELOCATION, not a re-measurement. Every rule that shipped in the retired table
    resolves out of the contract byte for byte, in the same order, under the same base."""
    assert _profile_digest(volatile_profile(source, PAGE_KIND_DETAIL)) == (
        _RETIRED_TABLE_DIGESTS[source])


def test_every_portal_declares_exactly_the_detail_surface() -> None:
    """Nine contracts, nine declarations, all of them `detail` — and none of them for a
    surface nobody has diffed. Writing an index profile takes an index diff (a deferred
    finding); guessing one here would be exactly the mis-application W2a-3d removed."""
    declared = contract_profiles().profiles

    assert set(contract_profiles().versions) == _FLEET
    assert set(declared) == {(source, PAGE_KIND_DETAIL) for source in _FLEET}


def test_an_undeclared_surface_falls_back_to_the_base_and_says_so() -> None:
    """Falling back to the portal-agnostic floor, never to that portal's detail rules —
    a detail selector on an index body is a measurement applied to a population it was
    never taken from."""
    for source in _FLEET:
        for page_kind in ("index", "map", "gazetteer", "snapshot", "archive", "none"):
            resolved = resolve_normalisation(source, page_kind)
            assert resolved.profile is BASE_PROFILE, (source, page_kind)
            assert resolved.normalizer_version == NORMALIZER_VERSION + BASE_PROFILE_SUFFIX


def test_every_shipped_selector_survives_the_load_time_gate() -> None:
    """The gate is only worth having if the fleet passes it: a shipped selector that is
    merely `safe` and not `usable` would no-op in silence."""
    unusable = [
        (source, page_kind, selector)
        for (source, page_kind), profile in contract_profiles().profiles.items()
        for selector in profile.css_selectors
        if not selector_is_usable(selector)
    ]
    assert unusable == []


# --------------------------------------------- 2. a bad declaration fails, loudly

def _block(**kwargs: object) -> dict[str, object]:
    return {"detail": {"base": "html", **kwargs}}


@pytest.mark.parametrize(("declared", "match"), [
    (_block(css_selectors=['span:contains("Datum")']), "not usable"),
    (_block(css_selectors=["div..a"]), "not usable"),
    (_block(css_selectors=["div[name=\"x"]), "not usable"),
    (_block(css_selectors=[""]), "non-string or empty"),
    (_block(css_selectors="div.a"), "must be a list"),
    (_block(json_pointers=["stats"]), "must start with"),
    (_block(strip_attributes=["data nonce"]), "attribute NAME"),
    (_block(css_selector=["div.a"]), "unknown key"),
    ({"detail": {"css_selectors": ["div.a"]}}, "base=None"),
    ({"detail": {"base": "sql", "css_selectors": []}}, "base='sql'"),
    ({"detail": ["div.a"]}, "a page_kind's volatile paths are a mapping"),
    ({"detial": {"base": "html"}}, "not a location_page_kind label"),
    (["/stats", "/image_urls"], "is a MAPPING of page_kind"),
    ("everything", "must be a mapping"),
])
def test_a_bad_declaration_is_refused_at_load_time(
    declared: object, match: str,
) -> None:
    """Every one of these reaches `normalise` as a SILENT no-op if it is not refused
    here — the module is silent by contract and cannot report any of them."""
    with pytest.raises(ProfileError, match=match):
        parse_volatile_paths(declared, where="t.yaml", page_kinds=contracts.PAGE_KINDS)


def test_the_contract_gate_refuses_it_too_and_names_the_file(tmp_path: Path) -> None:
    """The same refusal through `contracts.parse_contract`. Every contract on disk goes
    through it in this suite (`contracts.load_all`), and again at deploy time under
    `--load`, so a typo in a portal's volatile_paths fails the build rather than
    changing a measurement."""
    body = (CONTRACT_DIR / "maxima.yaml").read_text(encoding="utf-8")
    broken = body.replace(
        "    detail:\n      # Declared,",
        "    detail:\n      css_selectors: ['span:contains(\"Datum\")']\n      # Declared,",
        1)
    assert broken != body
    path = tmp_path / "maxima.yaml"
    path.write_text(broken, encoding="utf-8")

    with pytest.raises(contracts.ContractError, match="maxima.yaml.*not usable"):
        contracts.parse_contract(path)


def test_an_unreadable_contract_set_refuses_rather_than_silently_using_the_base(
    tmp_path: Path,
) -> None:
    """The degradation this refusal exists to prevent is invisible: every portal would
    quietly fall to the base profile, and a base-profile change rate looks exactly like
    a measurement. Both live callers wrap this in a never-raising warn-and-return
    (`record_payload_churn_if_enabled`, `append_payload_if_enabled`), so the instrument
    and the archive go quiet while the scrape carries on untouched."""
    with pytest.raises(ProfileError, match="no portal contracts under"):
        load_contract_profiles(tmp_path)

    (tmp_path / "broken.yaml").write_text("portal: [unclosed\n", encoding="utf-8")
    with pytest.raises(ProfileError, match="unreadable contract"):
        load_contract_profiles(tmp_path)


def test_a_flat_list_is_named_as_the_collapse_it_is() -> None:
    """The shape these files carried before this change. The message has to say WHY,
    because a flat list looks perfectly reasonable — it is only wrong once you know an
    index body is a list of other people's listings."""
    with pytest.raises(ProfileError, match="index bodies"):
        parse_volatile_paths(["/image_urls", "/broker"], where="t.yaml")


# ------------------------------------------------------- 3. the label cannot lie

def test_the_label_names_the_engine_and_the_contract_version(tmp_path: Path) -> None:
    """Two independent things move a normalised byte — this module's algorithm and the
    portal's declaration — so a label naming only one lets the other re-address the
    archive with no cohort break. Proved by moving the CONTRACT version alone: same
    engine, same selectors, a different cohort."""
    body = (CONTRACT_DIR / "idnes.yaml").read_text(encoding="utf-8")
    version = contract_profiles().versions["idnes"]
    assert resolve_normalisation("idnes", PAGE_KIND_DETAIL).normalizer_version == (
        f"{NORMALIZER_VERSION}{CONTRACT_PROFILE_SUFFIX}{version}")

    (tmp_path / "idnes.yaml").write_text(
        body.replace(f"contract_version: {version}",
                     f"contract_version: {version + 1}", 1),
        encoding="utf-8")
    bumped = load_contract_profiles(tmp_path)

    assert bumped.versions["idnes"] == version + 1
    # The profile is untouched by the bump — only the cohort it is counted in moves.
    assert bumped.profile("idnes", PAGE_KIND_DETAIL) == volatile_profile(
        "idnes", PAGE_KIND_DETAIL)


def test_the_projection_carries_the_declaration_into_the_db_row() -> None:
    """`portal_contracts.fetch_config` is where an operator reads this in psql. It is
    projected VERBATIM (contract_sha256 is taken over those same bytes), and the parsed
    form the runtime applies comes from exactly those bytes."""
    contract = contracts.parse_contract(CONTRACT_DIR / "idnes.yaml")

    declared = contract.fetch_config["persistence"]["volatile_paths"]
    assert set(declared) == {PAGE_KIND_DETAIL}
    assert declared[PAGE_KIND_DETAIL]["base"] == "html"
    assert contract.volatile_profiles[PAGE_KIND_DETAIL] == volatile_profile(
        "idnes", PAGE_KIND_DETAIL)


def test_every_contract_on_disk_parses_and_declares_a_profile() -> None:
    """`load_contract_profiles` reads three keys; `parse_contract` reads the whole file.
    They must agree about the profiles, or the gate would be validating something other
    than what the scrape applies."""
    registry = load_contract_profiles(CONTRACT_DIR)

    for contract in contracts.load_all(CONTRACT_DIR):
        assert registry.versions[contract.source] == contract.version
        assert contract.volatile_profiles, contract.source
        for page_kind, profile in contract.volatile_profiles.items():
            assert registry.profile(contract.source, page_kind) == profile
