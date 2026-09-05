"""A volatile profile belongs to a (source, page_kind), never to a portal.

Every profile in `payload_norm` was derived by diffing DETAIL pages (W2a-3b/3c).
Before this suite existed they were selected by source alone and therefore also
applied to INDEX bodies — a document that is a LIST of properties rather than one
property. Measured on live index pages while fixing that, the mis-application was
inert-but-unaudited on the three portals that archive index pages today (sreality's
26 JSON pointers removed 0 bytes; remax's 21 selectors matched only the shared
noscript/style; ceskereality's matched the shared chrome plus one hidden token) and
demonstrably NOT inert on bazos, whose detail-measured `div.inzeratyview` matches 21
nodes on one index page — one per listing card — with 1,497 bazos index bodies
sitting in `portal_raw_pages` awaiting the W2a-4 backfill.

What these tests pin, in the order the risk runs:

  1. DETAIL output did not move. `payload_sha256` is the archive's identity and
     `normalizer_version` is in `portal_payload_churn`'s PK, so a byte that moves
     here silently opens a cohort or duplicates a body. Pinned as digests over every
     committed detail fixture, computed under `payload_norm@3` before the change.
  2. No measured detail selector can reach an index body.
  3. An unmeasured surface falls back to the generic base, never to the portal's
     detail rules — and lands in its own `+base` cohort so the two instruments'
     numbers can never be averaged together.
  4. The failure this prevents, constructed: an "other listings" selector applied to
     a page whose every listing IS an other listing deletes the page's content, and
     two genuinely different index pages then hash alike. Silent, and it reads as a
     0% change rate — the best-looking possible result.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from location_data.payload_norm import (
    _HTML_ATTRS,
    _HTML_BASE,
    BASE_PROFILE,
    BASE_PROFILE_SUFFIX,
    NORMALIZER_VERSION,
    PAGE_KIND_DETAIL,
    PROFILE_DIGEST_CHARS,
    PROFILE_DIGEST_SUFFIX,
    Resolution,
    VolatileProfile,
    contract_profiles,
    normalise,
    normalizer_version_for,
    profile_digest,
    resolve_normalisation,
    volatile_profile,
)


def _cohort(source: str, page_kind: str = PAGE_KIND_DETAIL) -> str:
    """The label a declared surface must carry: the engine plus a digest of the profile
    the contract declares for it. Recomputed from the registry, never from the resolver
    under test."""
    profile = contract_profiles().profile(source, page_kind)
    assert profile is not None, (source, page_kind)
    return (f"{NORMALIZER_VERSION}{PROFILE_DIGEST_SUFFIX}"
            f"{profile_digest(profile)[:PROFILE_DIGEST_CHARS]}")

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_HTML = "text/html; charset=utf-8"
_JSON = "application/json"
_INDEX = "index"

# sha256 of the NORMALISED body of every committed detail fixture, taken under
# payload_norm@3 as it shipped — i.e. computed from the code this change replaces.
# They are the regression that matters: the profiles themselves are untouched, and a
# keying change must be a pure no-op on the surface they were measured for.
_DETAIL_DIGESTS: dict[tuple[str, str], str] = {
    ("bazos", "location_w2/bazos_detail.html"):
        "3260342567e793274f496d789d1bb4edf42bec6c6e54119347ea3d586afe252a",
    ("ceskereality", "location_w2/ceskereality_detail.html"):
        "835c64054ed413a1dea2aed988a39b70d797fe7d884fbf0359c3126b9e2b66de",
    ("idnes", "location_w2/idnes_detail.html"):
        "279fb3024d238ba2c1927b271b642019f9a647f0e605d35591a6ad8d02d4a8a1",
    ("maxima", "location_w2/maxima_detail.html"):
        "e0ea3cac396f989e62012b15a840fd843013b3b469dd5c05f3ca5360c2e639e8",
    ("mmreality", "location_w2/mmreality_detail.html"):
        "56799418831b5ac6d16933e5a16d70b2049aaabbe1fe2149d727089a9a21a94e",
    # Moved by contract realitymix@4 (W2-8): the modelled page's `data-address` was
    # restated to the comma shape the portal serves and its `data-form-address` moved to
    # the div that really carries it. The PROFILE is untouched, which is the property this
    # table exists to pin — a fixture edit moves the body's digest, never the cohort key.
    ("realitymix", "location_w2/realitymix_detail.html"):
        "34f9c30bc9dfe34c17ae1d213e53b449055561c1af8844508133d80ef2534a64",
    # W2-6 re-pinned: the FIXTURE moved, not the normaliser. remax's pinned body carried a
    # hand-written one-line `h2.pd-header__address`, which hid the nested `mapa` jump-link
    # every real remax page carries; the block is now copied verbatim from the archived
    # capture. The digest below is that file under the SAME payload_norm@3 profile.
    ("remax", "location_w2/remax_detail.html"):
        "2874e1972b6a7eee638167b24ebcc8ac24f991a1c709c78ab4b31591c00dcbc9",
    ("sreality", "location_w2/sreality_detail.json"):
        "58f277d33ee9bd08f243602a236934f0ecbd0b5b8a4654f1bdb3ea367c246af1",
    ("ceskereality", "location_w2a_refetch/ceskereality_a1.html"):
        "236fc60ca2597d8f393bf320d4548646e35aad464326c77dac5d4125a9ed10ba",
    ("ceskereality", "location_w2a_refetch/ceskereality_a2.html"):
        "236fc60ca2597d8f393bf320d4548646e35aad464326c77dac5d4125a9ed10ba",
    ("ceskereality", "location_w2a_refetch/ceskereality_b1.html"):
        "4b6495e634f4ac3e5cb8aa110583b0056f194b238d6d3fcd596a2919a992ea08",
    ("idnes", "location_w2a_refetch/idnes_a1.html"):
        "c5437bca4fcd8c133bce0f84a6ccacc219f190c42b9f9b1d45d809613b73cee6",
    ("idnes", "location_w2a_refetch/idnes_a2.html"):
        "c5437bca4fcd8c133bce0f84a6ccacc219f190c42b9f9b1d45d809613b73cee6",
    ("idnes", "location_w2a_refetch/idnes_b1.html"):
        "244c9fb8f70a266ddfbfc9690a1f497452652f802941c3843490ce1f58d7671f",
    ("mmreality", "location_w2a_refetch/mmreality_a1.html"):
        "1c44407509e96eb3dfe142ef68853c5ca3e63dea8cb02428db5b3cc09e7b44c1",
    ("mmreality", "location_w2a_refetch/mmreality_a2.html"):
        "1c44407509e96eb3dfe142ef68853c5ca3e63dea8cb02428db5b3cc09e7b44c1",
    ("mmreality", "location_w2a_refetch/mmreality_b1.html"):
        "ab0b2d4deb1fbeca3b068237cc594c9d4b6f8af3ec0e66aa6312f3cd0fc55464",
    ("realitymix", "location_w2a_refetch/realitymix_a1.html"):
        "26548ade553e452f7dba63e981fd36e5bff5ce148d7d16421716f074a7168578",
    ("realitymix", "location_w2a_refetch/realitymix_a2.html"):
        "26548ade553e452f7dba63e981fd36e5bff5ce148d7d16421716f074a7168578",
    ("realitymix", "location_w2a_refetch/realitymix_b1.html"):
        "24da1228858037c32a2811b8162dee5d211567cadbaf2f51de3aa50d603fcf30",
    ("remax", "location_w2a_refetch/remax_a1.html"):
        "f47783e1002f8be7c07d7436053e669d72ebcfb968e019fc4b70bd4938b3a493",
    ("remax", "location_w2a_refetch/remax_a2.html"):
        "f47783e1002f8be7c07d7436053e669d72ebcfb968e019fc4b70bd4938b3a493",
    ("remax", "location_w2a_refetch/remax_b1.html"):
        "8bf84d0958d755f407703477209ab1589ec98fbf50fd250a49a673311098102a",
    # The parser suite's own detail captures — different pages, and older, than the
    # W2 set above. They are committed detail bodies too, so they belong in the pin:
    # what it protects is the claim "detail output did not move", and a fixture left
    # out of it is a page the claim was never checked against.
    ("idnes", "portal_html/idnes_detail.html"):
        "6952b4899e1ecc7e3ddbcaa22fcf43ffb71f92a8096758fea0d94a6b359dc965",
    ("mmreality", "portal_html/mmreality_detail.html"):
        "2f97d89cbf626c931839708a9eea195052bfcd30c1faedce1c1a8ead202d473c",
    # Moved by the W2-8 PII scrub: the captured page still carried a real agent name in a
    # public repo, replaced with the placeholder by
    # `scripts/fetch_and_anonymize_fixtures.py --scrub-contacts --name …`.
    ("realitymix", "portal_html/realitymix_detail.html"):
        "0d8d874e7f2cba09f7acda8a53ef9f16d7052a1032b5609b4997eba73575ebd2",
    ("remax", "portal_html/remax_detail.html"):
        "d1557becec21011c9253421cbf4be0172f0f2d85fe2d20bdd09f7c96a7e51c8b",
}


# Where a committed DETAIL body can live, and how its portal is read off the name:
# `<portal>_detail.<ext>` for the parser/W2 captures, `<portal>_<round>.html` for the
# refetch rounds (every body in that directory is a detail refetch, by construction).
_DETAIL_FIXTURE_GLOBS: tuple[tuple[str, str], ...] = (
    ("location_w2", "*_detail.*"),
    ("portal_html", "*_detail.*"),
    ("location_w2a_refetch", "*.html"),
)


def _committed_detail_fixtures() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for directory, pattern in _DETAIL_FIXTURE_GLOBS:
        matched = sorted((_FIXTURES / directory).glob(pattern))
        # A renamed or moved directory must FAIL, not quietly shrink the corpus to
        # nothing — an empty glob is indistinguishable from "all pinned" downstream.
        assert matched, f"no detail fixtures under {directory}/{pattern}"
        found.update(
            (path.name.split("_")[0], f"{directory}/{path.name}") for path in matched
        )
    return found


def test_every_committed_detail_fixture_is_pinned() -> None:
    """The pin protects the claim "detail output did not move", so a detail fixture
    left out of it is a page that claim was never checked against.

    Discovered rather than listed, because a hand-maintained list cannot report what
    is missing from it: `tests/fixtures/portal_html/`'s three detail captures were
    absent from the pin exactly that way. Adding a fixture without a digest now fails
    here instead of silently narrowing what the pin covers."""
    assert _committed_detail_fixtures() == set(_DETAIL_DIGESTS)


@pytest.mark.parametrize(("source", "relpath"), sorted(_DETAIL_DIGESTS))
def test_detail_normalisation_is_byte_identical_to_payload_norm_3(
    source: str, relpath: str,
) -> None:
    """The one regression that matters. A moved byte here means every archived body
    re-addresses and every accumulating churn cohort has to be thrown away — which is
    exactly why NORMALIZER_VERSION is NOT bumped by this change."""
    path = _FIXTURES / relpath
    content_type = _JSON if path.suffix == ".json" else _HTML

    result = normalise(
        path.read_bytes(),
        content_type=content_type,
        volatile=volatile_profile(source, PAGE_KIND_DETAIL),
    )

    assert result.norm_sha256.hex() == _DETAIL_DIGESTS[(source, relpath)]


def test_an_unmeasured_surface_gets_the_generic_base_not_the_portals_detail_rules() -> None:
    """A (source, page_kind) nobody has diffed — and an unknown portal entirely."""
    for source in contract_profiles().versions:
        for page_kind in (_INDEX, "map", "gazetteer", "snapshot", "archive", "none"):
            assert volatile_profile(source, page_kind) is BASE_PROFILE, (source, page_kind)
    assert volatile_profile("a-portal-onboarded-next-week", PAGE_KIND_DETAIL) is BASE_PROFILE


def test_the_base_profile_is_only_the_portal_agnostic_shared_rules() -> None:
    """Why the fallback is the base and not "no stripping": every member is generic
    web plumbing (third-party analytics matched by src, page chrome, CSRF material,
    per-response attributes), so it cannot delete an address, a listing card or a map
    widget off a surface it was never measured against. Nothing measured leaks in."""
    assert BASE_PROFILE.css_selectors == _HTML_BASE
    assert BASE_PROFILE.strip_attributes == _HTML_ATTRS
    assert BASE_PROFILE.json_pointers == ()


def test_no_measured_detail_selector_can_reach_an_index_body() -> None:
    """The property, over every portal: what an index body is normalised with is a
    subset of the shared base, with no portal-specific rule in it."""
    for source in contract_profiles().versions:
        index_rules = set(volatile_profile(source, _INDEX).css_selectors)
        detail_only = set(
            volatile_profile(source, PAGE_KIND_DETAIL).css_selectors
        ) - set(_HTML_BASE)

        assert index_rules <= set(_HTML_BASE), source
        assert index_rules.isdisjoint(detail_only), source


def test_the_json_surfaces_normalise_identically_because_the_base_has_no_pointers() -> None:
    """sreality's index and bezrealitky's gazetteer are JSON. `_normalise_json` reads
    only `json_pointers` and BASE_PROFILE has none, so the fallback is inert there —
    which is why their live index cohorts do not move under this change (confirmed
    against a live 2.5 MB sreality index page: the 26 detail pointers removed 0 bytes,
    because they address an estate document and an index page is a list of them)."""
    body = b'{"results":[{"hash_id":1,"labels":["x"]}],"pagination":{"total":2}}'

    with_detail = normalise(
        body, content_type=_JSON, volatile=volatile_profile("sreality", PAGE_KIND_DETAIL),
    )
    with_base = normalise(
        body, content_type=_JSON, volatile=volatile_profile("sreality", _INDEX),
    )

    assert with_detail.norm_sha256 == with_base.norm_sha256


def _index_page(cards: str, *, tag: str = "main", attrs: str = "") -> bytes:
    """An index body: a page whose content IS a list of other people's listings.

    `tag`/`attrs` are separate so the CLOSING tag stays well-formed — a closing tag
    carrying attributes is invalid HTML, and a test that reasons about what a parser
    does should not hand it something a parser only tolerates by accident.
    """
    open_tag = f"<{tag} {attrs}>" if attrs else f"<{tag}>"
    return (
        "<html><head><style>p{}</style></head><body>"
        f"{open_tag}{cards}</{tag}>"
        "</body></html>"
    ).encode("utf-8")


def test_a_detail_similar_offers_selector_would_collapse_an_index_page() -> None:
    """The failure the (source, page_kind) keying exists to make impossible.

    `section.s-estates-slide` is ceskereality's "Podobné nemovitosti" rail, measured on
    a DETAIL page where it holds OTHER listings. On an index page every listing is an
    other listing, so the same selector can delete the page's entire content — and two
    genuinely different index pages then hash alike, which reads downstream as a 0%
    change rate rather than as an error.

    CONSTRUCTED, not a claim about today's template: applied to five live ceskereality
    index bodies (two www, three region-host slices) that selector matched 0 nodes. The
    point is that nothing in the old keying made that a property rather than luck.
    """
    slide = {"tag": "section", "attrs": 'class="s-estates-slide"'}
    page_a = _index_page("<article>Korunní 1, Praha 2</article>", **slide)
    page_b = _index_page("<article>Veveří 9, Brno</article>", **slide)

    detail_profile = volatile_profile("ceskereality", PAGE_KIND_DETAIL)
    collapsed = {
        normalise(p, content_type=_HTML, volatile=detail_profile).norm_sha256
        for p in (page_a, page_b)
    }
    assert len(collapsed) == 1, "the detail profile deletes both pages' whole content"

    index_profile = volatile_profile("ceskereality", _INDEX)
    kept = {
        normalise(p, content_type=_HTML, volatile=index_profile).norm_sha256
        for p in (page_a, page_b)
    }
    assert len(kept) == 2
    assert b"Korunn\xc3\xad 1" in normalise(
        page_a, content_type=_HTML, volatile=index_profile).norm_bytes


def test_a_per_listing_detail_node_repeats_once_per_card_on_an_index() -> None:
    """bazos's `div.inzeratyview` is the "Vidělo: N lidí" counter — one node on a
    detail page, and 21 on a live index page (one per card). Measured, not supposed:
    1,497 bazos index bodies are in portal_raw_pages waiting for the W2a-4 backfill,
    which normalises through this same resolver."""
    cards = "".join(
        f'<div class="inzerat"><h2>Byt {i}</h2>'
        f'<div class="inzeratyview">Vidělo: {i} lidí</div></div>'
        for i in range(1, 4)
    )
    body = _index_page(cards)

    on_detail = normalise(
        body, content_type=_HTML, volatile=volatile_profile("bazos", PAGE_KIND_DETAIL),
    )
    on_index = normalise(
        body, content_type=_HTML, volatile=volatile_profile("bazos", _INDEX),
    )

    assert b"inzeratyview" not in on_detail.norm_bytes
    assert on_index.norm_bytes.count(b"inzeratyview") == 3
    assert b"Byt 2" in on_index.norm_bytes


def test_the_cohort_label_names_both_axes_that_can_move_a_byte() -> None:
    """`normalizer_version` is in portal_payload_churn's PK. Two independent things
    can move a normalised byte — the ENGINE (this module's algorithm) and the PROFILE
    (what the portal's contract declares) — so both are named, or one of them could move
    a permanent content address with no cohort break to show for it. The profile half is
    a DIGEST of the rules, never the contract_version carrying them: a version moves for
    extraction reasons, and orphaning a surface's counters for a locator fix is exactly
    the waste NORMALIZER_VERSION refuses on the engine axis.

    A surface no contract declares is a different instrument again: the generic base,
    which belongs to the normaliser and is identical under every contract version. So
    it keeps `+base` across the move to contracts, and the index cohorts accumulating
    today are not thrown away by it."""
    registry = contract_profiles()
    for source in registry.versions:
        assert normalizer_version_for(source, PAGE_KIND_DETAIL) == _cohort(source)
        assert normalizer_version_for(source, _INDEX) == (
            NORMALIZER_VERSION + BASE_PROFILE_SUFFIX)

    # A declaration that exists but is EMPTY is a measurement ("nothing here churns"),
    # not a fallback — bezrealitky's null detail profile must not be relabelled base.
    assert volatile_profile("bezrealitky", PAGE_KIND_DETAIL).css_selectors == ()
    assert normalizer_version_for("bezrealitky", PAGE_KIND_DETAIL) == _cohort(
        "bezrealitky")

    assert normalizer_version_for("unknown-portal", PAGE_KIND_DETAIL) == (
        NORMALIZER_VERSION + BASE_PROFILE_SUFFIX)


def test_the_profile_and_its_cohort_label_are_resolved_from_one_surface() -> None:
    """The pair is one answer, so the stamp can never describe a profile that was not
    the one applied. Asking the two questions separately is what let a row normalised
    under a caller's profile be stamped from the profile TABLE — a permanent content
    address explained by an instrument that never touched it."""
    for source in (*contract_profiles().versions, "a-portal-onboarded-next-week"):
        for page_kind in (PAGE_KIND_DETAIL, _INDEX, "map", "gazetteer"):
            resolved = resolve_normalisation(source, page_kind)

            assert resolved.profile == volatile_profile(source, page_kind)
            assert resolved.normalizer_version == normalizer_version_for(
                source, page_kind)
            # The one question that separates the two instruments: the label says
            # `+base` exactly when the profile IS the fallback.
            assert (resolved.profile is BASE_PROFILE) == (
                resolved.normalizer_version.endswith(BASE_PROFILE_SUFFIX)
            ), (source, page_kind)

    # Empty is not missing: bezrealitky's detail profile is a MEASUREMENT that found
    # nothing to strip, so it is not the fallback and keeps the bare version.
    bezrealitky = resolve_normalisation("bezrealitky", PAGE_KIND_DETAIL)
    assert bezrealitky.profile == VolatileProfile()
    assert bezrealitky.profile is not BASE_PROFILE
    assert bezrealitky.normalizer_version.startswith(
        NORMALIZER_VERSION + PROFILE_DIGEST_SUFFIX)


def test_a_normaliser_bump_carries_through_the_pair_together() -> None:
    """`version=` reaches the label without touching the profile — the cohort moves,
    the projection does not, which is what a bump means."""
    digest = profile_digest(
        contract_profiles().profile("idnes", PAGE_KIND_DETAIL))[:PROFILE_DIGEST_CHARS]
    bumped = resolve_normalisation("idnes", PAGE_KIND_DETAIL, "payload_norm@99")

    assert bumped.normalizer_version == (
        f"payload_norm@99{PROFILE_DIGEST_SUFFIX}{digest}")
    assert bumped.profile == volatile_profile("idnes", PAGE_KIND_DETAIL)
    assert resolve_normalisation("idnes", _INDEX, "payload_norm@99") == Resolution(
        profile=BASE_PROFILE,
        normalizer_version="payload_norm@99" + BASE_PROFILE_SUFFIX,
    )


def test_the_page_kind_label_agrees_across_the_scraper_boundary() -> None:
    """payload_norm cannot import scraper.db (the churn hook's import is deferred so a
    flag-off scrape never pays for location_data), so the label is spelled in both. A
    drift would not raise — it would silently send every detail body to BASE_PROFILE."""
    from scraper import db

    assert db.DETAIL_PAGE_KIND == PAGE_KIND_DETAIL
    assert db.INDEX_PAGE_KIND == _INDEX
