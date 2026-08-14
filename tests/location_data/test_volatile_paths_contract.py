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

from pathlib import Path

import pytest

from location_data import contracts
from location_data.payload_norm import (
    BASE_PROFILE,
    BASE_PROFILE_SUFFIX,
    CONTRACT_DIR,
    NORMALIZER_VERSION,
    PAGE_KIND_DETAIL,
    PROFILE_DIGEST_CHARS,
    PROFILE_DIGEST_SUFFIX,
    ProfileError,
    VolatileProfile,
    contract_profiles,
    load_contract_profiles,
    parse_volatile_paths,
    profile_digest,
    resolve_normalisation,
    selector_is_usable,
    volatile_profile,
)

_FLEET = frozenset({
    "sreality", "bezrealitky", "bazos", "idnes", "mmreality",
    "remax", "ceskereality", "realitymix", "maxima",
})


# Computed from `MEASURED_VOLATILE_PROFILES` as it stood on origin/main BEFORE the
# relocation — i.e. from the code the contracts replace. Regenerating one of these to
# make the suite pass is never the fix: it would silently move a permanent content
# address and make every churn number measured so far incomparable. A deliberate
# profile change re-measures, opens a clean cohort by moving the digest (the label IS
# these first 8 hex, W2a-3e) and updates the pin in the same reviewed diff.
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
    assert profile_digest(volatile_profile(source, PAGE_KIND_DETAIL)) == (
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

def _profile_of(source: str, directory: Path) -> VolatileProfile | None:
    return load_contract_profiles(directory).profile(source, PAGE_KIND_DETAIL)


def _cohort(profile: VolatileProfile) -> str:
    return (f"{NORMALIZER_VERSION}{PROFILE_DIGEST_SUFFIX}"
            f"{profile_digest(profile)[:PROFILE_DIGEST_CHARS]}")


@pytest.mark.parametrize("source", sorted(_RETIRED_TABLE_DIGESTS))
def test_the_label_is_the_digest_of_the_profile_that_was_applied(source: str) -> None:
    """The cohort key IS the pin above: the label's 8 hex are the first 8 of the digest
    computed under the retired table. So the same fixture that proves the projection did
    not move also proves the cohort naming it did not move, and neither can drift alone."""
    resolved = resolve_normalisation(source, PAGE_KIND_DETAIL)

    assert resolved.normalizer_version == (
        f"{NORMALIZER_VERSION}{PROFILE_DIGEST_SUFFIX}"
        f"{_RETIRED_TABLE_DIGESTS[source][:PROFILE_DIGEST_CHARS]}")
    assert resolved.profile is not BASE_PROFILE
    assert not resolved.normalizer_version.endswith(BASE_PROFILE_SUFFIX)


def test_an_extraction_only_contract_version_bump_leaves_the_cohort_alone(
    tmp_path: Path,
) -> None:
    """THE reason the label is a digest and not `contract_version`. A locator fix bumps
    the version — ceskereality and realitymix each took two such bumps in the fortnight
    before this shipped — while `persistence.volatile_paths` does not move a byte. Keyed
    on the version, every one of those would land in portal_payload_churn's PK (mig 402),
    orphan that surface's accumulated counters and restart the readout at fetches=1, for
    a projection that is identical. The storage sign-off rests on those counters."""
    body = (CONTRACT_DIR / "idnes.yaml").read_text(encoding="utf-8")
    version = contract_profiles().versions["idnes"]
    (tmp_path / "idnes.yaml").write_text(
        body.replace(f"contract_version: {version}",
                     f"contract_version: {version + 1}", 1),
        encoding="utf-8")

    bumped = load_contract_profiles(tmp_path)
    shipped = volatile_profile("idnes", PAGE_KIND_DETAIL)

    assert bumped.versions["idnes"] == version + 1
    assert bumped.profile("idnes", PAGE_KIND_DETAIL) == shipped
    assert _cohort(bumped.profile("idnes", PAGE_KIND_DETAIL)) == _cohort(shipped)
    assert _cohort(shipped) == resolve_normalisation(
        "idnes", PAGE_KIND_DETAIL).normalizer_version


def test_an_edit_to_the_declaration_opens_a_clean_cohort(tmp_path: Path) -> None:
    """The other direction, and the one that must never be missed: the rules moved, so
    every body normalised after it is a DIFFERENT projection under a permanent content
    address, and it has to be counted apart from what came before — with no version bump
    anywhere, since `persistence` is not what `contract_version` governs."""
    body = (CONTRACT_DIR / "idnes.yaml").read_text(encoding="utf-8")
    edited = body.replace('- ".advertisement"', '- ".advertisement"\n        - "aside.ad"', 1)
    assert edited != body
    (tmp_path / "idnes.yaml").write_text(edited, encoding="utf-8")

    changed = _profile_of("idnes", tmp_path)
    shipped = volatile_profile("idnes", PAGE_KIND_DETAIL)

    assert load_contract_profiles(tmp_path).versions["idnes"] == (
        contract_profiles().versions["idnes"])
    assert changed != shipped
    assert _cohort(changed) != _cohort(shipped)


def test_the_projection_carries_the_declaration_into_the_db_row() -> None:
    """`portal_contracts.fetch_config` is where an operator reads this in psql. It is
    projected VERBATIM, and the parsed form the runtime applies comes from exactly those
    bytes — from the FILE, never from this projection."""
    contract = contracts.parse_contract(CONTRACT_DIR / "idnes.yaml")

    declared = contract.fetch_config["persistence"]["volatile_paths"]
    assert set(declared) == {PAGE_KIND_DETAIL}
    assert declared[PAGE_KIND_DETAIL]["base"] == "html"
    assert contract.volatile_profiles[PAGE_KIND_DETAIL] == volatile_profile(
        "idnes", PAGE_KIND_DETAIL)


# ------------------------------------- 4. the hash covers extraction, and only that

def test_persistence_is_outside_contract_sha256(tmp_path: Path) -> None:
    """`contract_sha256` is the immutability gate on the ENTRIES, and a mismatch demands a
    `contract_version` bump — which re-stamps `extractor_version` and `contract_entry_id`
    and so RE-INSERTS every claim the next incremental scan re-walks (5.1M rows / 2.6 GB
    in August 2026). Archive configuration must not be able to spend that, so an edit
    inside `persistence:` is not a hash change; an edit anywhere else still is."""
    path = CONTRACT_DIR / "idnes.yaml"
    body = path.read_bytes()
    shipped = contracts.contract_body_hash(body)

    edited = body.replace(b'- ".advertisement"', b'- ".advertisement"\n        - "aside.ad"', 1)
    assert edited != body
    assert contracts.contract_body_hash(edited) == shipped

    # …and the profile the runtime resolves DID move, so "not hashed" is not "not read".
    (tmp_path / "idnes.yaml").write_bytes(edited)
    assert _profile_of("idnes", tmp_path) != volatile_profile("idnes", PAGE_KIND_DETAIL)

    # An extraction byte is still governed, or the gate would be decorative.
    entry_edit = body.replace(b"id.det.legacy_pin", b"id.det.legacy_pin_2", 1)
    assert entry_edit != body
    assert contracts.contract_body_hash(entry_edit) != shipped


def test_the_persistence_block_ends_where_the_next_top_level_key_begins() -> None:
    """The exclusion is a BLOCK, not a line: it must swallow `volatile_paths`, the nested
    narrative comments and `version_cap`, and stop dead at the next unindented key. Proved
    by hashing a file whose persistence block is replaced wholesale — same hash — and one
    whose NEXT top-level block is edited by a single character — different hash."""
    body = (CONTRACT_DIR / "maxima.yaml").read_bytes()
    head, _, tail = body.partition(b"\npersistence:\n")
    assert tail, "maxima.yaml no longer has a top-level persistence block"
    rest = tail.partition(b"\nexclusion_zones:")[2]

    swapped = head + b"\npersistence:\n  volatile_paths: {}\n\nexclusion_zones:" + rest
    assert contracts.contract_body_hash(swapped) == contracts.contract_body_hash(body)

    moved = body.replace(b"exclusion_zones:", b"exclusion_zones: ", 1)
    assert contracts.contract_body_hash(moved) != contracts.contract_body_hash(body)


def test_two_files_naming_one_portal_are_refused_by_both_loaders(tmp_path: Path) -> None:
    """Neither loader may resolve a portal key by key across files: the version would come
    from one file and each profile from whichever file declared that page_kind last, so a
    row could carry provenance from a contract that never supplied the rules it was
    normalised under. `portal_contracts` has `unique (source, version)`, so the duplicate
    is not reachable today — it is refused because it is a permanent content address."""
    body = (CONTRACT_DIR / "idnes.yaml").read_text(encoding="utf-8")
    (tmp_path / "idnes.yaml").write_text(body, encoding="utf-8")
    (tmp_path / "zz_idnes_shadow.yaml").write_text(
        body.replace("contract_version: 1", "contract_version: 99", 1), encoding="utf-8")

    with pytest.raises(ProfileError, match="already declared by"):
        load_contract_profiles(tmp_path)
    with pytest.raises(contracts.ContractError, match="already declared by"):
        contracts.load_all(tmp_path)


def test_the_runtime_loader_checks_the_page_kind_enum_too(tmp_path: Path) -> None:
    """The two loaders must agree about what a page_kind is. A typo'd key passes YAML,
    declares a surface that does not exist, and leaves the surface it was MEANT for on the
    base profile — honestly labelled `+base`, and silent about the dead declaration."""
    body = (CONTRACT_DIR / "idnes.yaml").read_text(encoding="utf-8")
    (tmp_path / "idnes.yaml").write_text(
        body.replace("    detail:\n", "    detial:\n", 1), encoding="utf-8")

    with pytest.raises(ProfileError, match="not a location_page_kind label"):
        load_contract_profiles(tmp_path)
    with pytest.raises(contracts.ContractError, match="not a location_page_kind label"):
        contracts.parse_contract(tmp_path / "idnes.yaml")


def test_the_contract_directory_is_read_at_call_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`CONTRACT_DIR` bound as a default argument would be read once at import, and a test
    that redirected the module attribute would go on loading the shipped contracts while
    reporting that it had not."""
    (tmp_path / "idnes.yaml").write_text(
        "portal: idnes\ncontract_version: 7\npersistence:\n  volatile_paths: {}\n",
        encoding="utf-8")
    monkeypatch.setattr("location_data.payload_norm.CONTRACT_DIR", tmp_path)

    registry = load_contract_profiles()

    assert registry.versions == {"idnes": 7}
    assert registry.profiles == {}


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
