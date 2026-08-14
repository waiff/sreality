"""The W2a-0 normaliser: deterministic, volatile-stripping, and unkillable.

02-portal-contracts.md section 2.3.2 P1 makes the raw-vs-normalised change rate
the gate on index archiving, so the normaliser has to be worth trusting as a
measuring instrument: the same body must always hash the same way (key order and
whitespace are not content), the declared volatile bits must actually disappear,
re-normalising must be a fixed point (otherwise `norm_changes` counts the
normaliser's own instability), and NO body may make it raise — it runs inside a
live scrape.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from location_data.payload_norm import (
    DEFAULT_VOLATILE_PROFILES,
    NORMALIZER_VERSION,
    VolatileProfile,
    normalise,
    selector_is_safe,
    selector_is_usable,
    sniff_content_type,
)

_JSON = "application/json"
_HTML = "text/html; charset=utf-8"
_NONE = VolatileProfile()


def _norm(body: bytes, content_type: str = _JSON, volatile: VolatileProfile = _NONE):
    return normalise(body, content_type=content_type, volatile=volatile)


def test_key_order_and_whitespace_are_not_content() -> None:
    a = b'{"b": 2, "a": {"y": 1, "x": [1, 2]}}'
    b = b'{\n  "a": {"x": [1,2],\n "y": 1},\n  "b": 2\n}'

    ra, rb = _norm(a), _norm(b)

    assert ra.norm_sha256 == rb.norm_sha256
    assert ra.raw_sha256 != rb.raw_sha256
    assert ra.norm_bytes == b'{"a":{"x":[1,2],"y":1},"b":2}'


def test_json_pointer_stripping_removes_only_the_declared_paths() -> None:
    body = json.dumps({
        "price": 100,
        "stats": {"views": 41},
        "user": {"name": "K", "image": "https://sdn.cz/a?sig=1"},
        "advert_images": [
            {"id": 1, "url": "https://sdn.cz/1?sig=a"},
            {"id": 2, "url": "https://sdn.cz/2?sig=b"},
        ],
    }).encode("utf-8")
    churned = json.dumps({
        "price": 100,
        "stats": {"views": 99},
        "user": {"name": "K", "image": "https://sdn.cz/a?sig=2"},
        "advert_images": [
            {"id": 1, "url": "https://sdn.cz/1?sig=c"},
            {"id": 2, "url": "https://sdn.cz/2?sig=d"},
        ],
    }).encode("utf-8")
    profile = VolatileProfile(json_pointers=(
        "/stats", "/user/image", "/advert_images/-/url",
    ))

    kept, dropped = _norm(body, volatile=_NONE), _norm(body, volatile=profile)

    assert kept.norm_sha256 != _norm(churned).norm_sha256
    assert dropped.norm_sha256 == _norm(churned, volatile=profile).norm_sha256
    assert b"stats" not in dropped.norm_bytes
    assert b"sdn.cz" not in dropped.norm_bytes
    assert b'"id":1' in dropped.norm_bytes


def test_json_pointer_paths_that_do_not_exist_are_no_ops() -> None:
    body = b'{"a":1}'
    profile = VolatileProfile(json_pointers=(
        "/nope", "/a/deeper/still", "/list/4/x", "no-leading-slash", "/a/-/x",
    ))

    assert _norm(body, volatile=profile).norm_sha256 == _norm(body).norm_sha256


def test_html_selectors_and_attributes_are_removed() -> None:
    def page(token: str, nonce: str, views: str) -> bytes:
        return (
            f'<html><head><script nonce="{nonce}" src="//x.gemius.pl/a.js"></script>'
            f'</head><body><div class="advertisement">buy</div>'
            f'<input type="hidden" name="form[_token]" value="{token}">'
            f'<div class="inzeratyview">{views}</div>'
            f'<h1 nonce="{nonce}">Byt 3+1</h1></body></html>'
        ).encode("utf-8")

    profile = VolatileProfile(
        css_selectors=(
            'script[src*="gemius"]', ".advertisement",
            'input[name*="_token"]', "div.inzeratyview",
        ),
        strip_attributes=("nonce",),
    )

    first = normalise(page("t1", "n1", "40"), content_type=_HTML, volatile=profile)
    second = normalise(page("t2", "n2", "41"), content_type=_HTML, volatile=profile)

    assert first.raw_sha256 != second.raw_sha256
    assert first.norm_sha256 == second.norm_sha256
    assert b"Byt 3+1" in first.norm_bytes
    for gone in (b"gemius", b"advertisement", b"_token", b"nonce", b"inzeratyview"):
        assert gone not in first.norm_bytes


def test_html_whitespace_runs_collapse() -> None:
    tight = normalise(b"<div> <p>a b</p> </div>", content_type=_HTML, volatile=_NONE)
    loose = normalise(
        b"<div>\n\t  <p>a \n   b</p>\n</div>", content_type=_HTML, volatile=_NONE,
    )

    assert tight.norm_sha256 == loose.norm_sha256


def test_normalise_is_idempotent_on_every_path() -> None:
    profile_json = VolatileProfile(json_pointers=("/stats",))
    profile_html = VolatileProfile(
        css_selectors=(".advertisement",), strip_attributes=("nonce",),
    )
    cases = [
        (b'{"b":1,"a":{"z":2},"stats":{"v":3}}', _JSON, profile_json),
        (b'<html><body><div class="advertisement">x</div>'
         b'<p nonce="n">  hi  there </p></body></html>', _HTML, profile_html),
        (b"\x00\x01 raw   bytes \x02", "application/octet-stream", _NONE),
    ]

    for body, content_type, profile in cases:
        once = normalise(body, content_type=content_type, volatile=profile)
        twice = normalise(
            once.norm_bytes, content_type=content_type, volatile=profile,
        )
        assert once.norm_sha256 == twice.norm_sha256, content_type


def test_undecodable_and_malformed_bodies_degrade_instead_of_raising() -> None:
    # cp1250 is what a mis-declared Czech portal page would arrive as; `ň` is
    # a single byte there and not valid UTF-8.
    cp1250 = "Byt 3+1, Plzeň".encode("cp1250")
    assert b"\xf2" in cp1250
    bodies = [
        (cp1250, _JSON),
        (cp1250, _HTML),
        (b'{"truncated": ', _JSON),
        (b"", _JSON),
        (b"", _HTML),
        (b"\xff\xfe\x00nonsense", "application/octet-stream"),
    ]

    for body, content_type in bodies:
        result = normalise(body, content_type=content_type, volatile=_NONE)
        assert len(result.raw_sha256) == 32
        assert len(result.norm_sha256) == 32
        assert result.byte_size == len(body)


def test_degraded_json_body_still_separates_on_content() -> None:
    a = normalise(b'{"truncated": 1', content_type=_JSON, volatile=_NONE)
    b = normalise(b'{"truncated": 2', content_type=_JSON, volatile=_NONE)

    assert a.norm_sha256 != b.norm_sha256


def test_a_245kb_body_normalises() -> None:
    # mmreality's observed detail size (02 section 2.3.2's storage projection).
    payload = {"items": [{"i": i, "t": "x" * 200} for i in range(1160)]}
    body = json.dumps(payload).encode("utf-8")
    assert 240_000 < len(body) < 260_000

    result = _norm(body, volatile=VolatileProfile(json_pointers=("/items/-/t",)))

    assert result.byte_size == len(body)
    assert result.norm_byte_size < result.byte_size
    assert b'"t"' not in result.norm_bytes


def test_sizes_and_hashes_are_reported_over_the_right_bytes() -> None:
    body = b'{ "a" : 1 }'
    result = _norm(body)

    assert result.byte_size == len(body)
    assert result.norm_byte_size == len(result.norm_bytes) == len(b'{"a":1}')
    assert result.raw_sha256 != result.norm_sha256


def test_content_type_sniffing() -> None:
    assert sniff_content_type(b'\n  {"a":1}') == "application/json"
    assert sniff_content_type(b"[1,2]") == "application/json"
    assert sniff_content_type(b"\xef\xbb\xbf<!doctype html><p>x") == "text/html"
    assert sniff_content_type(b"plain") == "application/octet-stream"
    assert sniff_content_type(b"") == "application/octet-stream"


def test_every_portal_has_a_profile_and_the_version_is_stamped() -> None:
    sources = {
        "sreality", "bazos", "bezrealitky", "idnes", "mmreality",
        "remax", "ceskereality", "realitymix", "maxima",
    }

    assert sources == set(DEFAULT_VOLATILE_PROFILES)
    assert NORMALIZER_VERSION.startswith("payload_norm@")


def test_default_profiles_apply_cleanly_to_their_portal_body_kind() -> None:
    """A selector Postgres-side typo would silently strip nothing; a bad one
    would raise inside the scrape. Exercise every shipped profile once."""
    html = (
        b'<html><head><meta name="csrf-token" content="x">'
        b'<link rel="preload" href="/a.js"><style>p{}</style></head>'
        b'<body><noscript>n</noscript><h1>Byt</h1>'
        b'<div class="inzeratyview">7</div><div class="advertisement">ad</div>'
        b'</body></html>'
    )
    json_body = json.dumps({"price": 1, "stats": {"v": 2}}).encode("utf-8")

    for source, profile in DEFAULT_VOLATILE_PROFILES.items():
        if not profile.css_selectors:
            result = normalise(json_body, content_type=_JSON, volatile=profile)
            assert b'"price":1' in result.norm_bytes, source
            continue
        result = normalise(html, content_type=_HTML, volatile=profile)
        # Shared across every HTML profile: page chrome + CSRF material.
        assert b"Byt" in result.norm_bytes, source
        assert b"csrf-token" not in result.norm_bytes, source
        assert b"preload" not in result.norm_bytes, source
        assert b"noscript" not in result.norm_bytes, source

    idnes = normalise(
        html, content_type=_HTML, volatile=DEFAULT_VOLATILE_PROFILES["idnes"],
    )
    bazos = normalise(
        html, content_type=_HTML, volatile=DEFAULT_VOLATILE_PROFILES["bazos"],
    )
    assert b"advertisement" not in idnes.norm_bytes
    assert b"inzeratyview" not in bazos.norm_bytes


def test_sreality_profile_strips_the_hashing_module_volatile_keys() -> None:
    """Every volatile key set scraper.hashing proved against live sreality churn
    must have a pointer here. Under-stripping inflates the measured change rate,
    which is the direction that corrupts the P2 storage decision."""
    from scraper import hashing

    pointers = set(DEFAULT_VOLATILE_PROFILES["sreality"].json_pointers)
    # `rusReply` is the legacy camelCase alias of `rus_reply`, only ever present
    # in pre-v1 archived raw_json; the live API never emits it.
    expected = (
        {f"/{k}" for k in hashing.VOLATILE_TOP_KEYS - {"rusReply"}}
        | {f"/params/{k}" for k in hashing.VOLATILE_PARAM_KEYS}
        | {f"/_embedded/{k}" for k in hashing.VOLATILE_EMBEDDED_KEYS}
        | {f"/user/{k}" for k in hashing.VOLATILE_USER_KEYS}
        | {f"/premise/{k}" for k in hashing.VOLATILE_PREMISE_KEYS}
        | {f"/premise/company/{k}" for k in hashing.VOLATILE_PREMISE_COMPANY_KEYS}
    )

    assert expected <= pointers
    # The sdn_*_attachment_url family, as hashing.py's prefix+suffix rule rather
    # than the one member that happens to be in today's fixtures.
    glob = f"/{hashing._ATTACHMENT_URL_PREFIX}*{hashing._ATTACHMENT_URL_SUFFIX}"
    assert glob in pointers
    # hashing.VOLATILE_ITEM_NAMES is a value predicate (drop the `items` entry
    # NAMED "Aktualizace") that a JSON pointer cannot express; its sibling — the
    # per-item `topped` flag — is covered by the array wildcard.
    assert "/items/-/topped" in pointers


def test_key_glob_strips_the_whole_attachment_url_family() -> None:
    body = json.dumps({
        "sdn_energy_performance_attachment_url": "https://sdn.cz/a?sig=1",
        "sdn_floorplan_attachment_url": "https://sdn.cz/b?sig=1",
        "sdn_keep_me": "not an attachment",
        "name": "Byt 3+1",
    }).encode("utf-8")
    other = json.dumps({
        "sdn_energy_performance_attachment_url": "https://sdn.cz/a?sig=2",
        "sdn_floorplan_attachment_url": "https://sdn.cz/b?sig=2",
        "sdn_keep_me": "not an attachment",
        "name": "Byt 3+1",
    }).encode("utf-8")
    profile = DEFAULT_VOLATILE_PROFILES["sreality"]

    a = normalise(body, content_type=_JSON, volatile=profile)
    b = normalise(other, content_type=_JSON, volatile=profile)

    assert a.raw_sha256 != b.raw_sha256
    assert a.norm_sha256 == b.norm_sha256
    assert b"sdn_keep_me" in a.norm_bytes
    assert b"attachment_url" not in a.norm_bytes


def test_key_glob_only_matches_both_ends() -> None:
    profile = VolatileProfile(json_pointers=("/a*z",))
    doc = {"az": 1, "abz": 2, "abc": 3, "xbz": 4, "a": 5}

    result = normalise(
        json.dumps(doc).encode("utf-8"), content_type=_JSON, volatile=profile,
    )

    assert json.loads(result.norm_bytes) == {"abc": 3, "xbz": 4, "a": 5}


def test_normaliser_is_pure() -> None:
    """No DB, no network, no clock — the module must be safe to call inside a
    drain batch and must produce the same hash on any runner."""
    module = Path(__file__).resolve().parents[2] / "location_data" / "payload_norm.py"
    tree = ast.parse(module.read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert imported <= {
        "__future__", "hashlib", "json", "re", "dataclasses", "typing", "selectolax",
    }, sorted(imported)


# --- @2 (W2a-3b): selector_is_safe + the measured idnes/ceskereality/realitymix profiles ---


def test_selector_is_safe_allows_the_verified_pseudo_classes() -> None:
    assert selector_is_safe(".advertisement")
    assert selector_is_safe("input[name=\"tshee\"]")
    assert selector_is_safe("footer div.absolute.bottom-2.right-2")
    assert selector_is_safe("span.i-info__title:not(.x)")
    assert selector_is_safe("li:first-child")
    assert selector_is_safe("li:nth-child(2)")


def test_selector_is_safe_rejects_unimplemented_pseudo_classes() -> None:
    # The exact selector that segfaults selectolax 0.4.10 against a real
    # document (see the module-level comment above _PSEUDO_RE).
    assert not selector_is_safe('span.i-info__title:contains("Datum")')
    assert not selector_is_safe("p::before")


def test_selector_is_safe_ignores_colons_that_are_not_pseudo_classes() -> None:
    """A pseudo-class colon never appears inside `[...]`, and an ESCAPED colon is
    part of a class name. A portal's own href/src value carries the first
    (`mailto:`, `tel:`); Tailwind's responsive variants carry the second
    (`.md\\:flex`) — and realitymix, the portal whose measured volatile node is
    already a Tailwind utility stack, is the likeliest source of one. Literal
    string scanning misreads either as an unlisted pseudo-class and silently
    no-ops the whole selector — under-stripping, the direction the module's
    docstring calls out as dangerous."""
    assert selector_is_safe('a[href^="mailto:info"]')
    assert selector_is_safe('a[href*="tel:"]')
    assert selector_is_safe('script[src*="imedia.cz"]')
    assert selector_is_safe('a[href^="tel:"]:not(.keep)')
    assert selector_is_safe(r".md\:flex")
    assert selector_is_safe(r"footer div.lg\:hidden.bottom-2")

    # A real pseudo-class outside the brackets is still caught, escaped or not.
    assert not selector_is_safe('div[data-x="a:b"]:contains("y")')
    assert not selector_is_safe(r".md\:flex:contains(\:)")


def test_an_unsafe_selector_in_a_profile_is_a_no_op_not_a_crash() -> None:
    """Regression for the selectolax segfault: an unimplemented pseudo-class
    reaching `.css()` kills the process, not raises — so `normalise` must
    never hand one to selectolax. Mixing a safe and an unsafe selector in one
    profile proves the rest of the profile (selectors AND attributes) still
    runs while the unsafe one is skipped."""
    html = (
        b'<html><body><h1  nonce="n">Byt   3+1</h1>'
        b'<div class="advertisement">ad</div>'
        b'<span class="i-info__title">Datum: 1.1.</span>'
        b'</body></html>'
    )
    profile = VolatileProfile(
        css_selectors=(".advertisement", 'span.i-info__title:contains("Datum")'),
        strip_attributes=("nonce",),
    )

    result = normalise(html, content_type=_HTML, volatile=profile)

    assert b"advertisement" not in result.norm_bytes
    assert b"Datum" in result.norm_bytes  # the unsafe selector never ran
    assert b"Byt 3+1" in result.norm_bytes
    assert b"nonce" not in result.norm_bytes


def test_a_malformed_selector_costs_only_itself_not_the_whole_body() -> None:
    """`selector_is_safe` screens pseudo-classes, not syntax: a typo'd selector
    passes it and RAISES inside `.css()`. Caught around the loop, that one typo
    would drop the entire body to the raw-bytes fallback — the portal's change
    rate jumps to ~100% and the storage projection is signed off a corrupt
    number, silently, since `normalise` never raises. It has to cost exactly one
    rule, so the good selectors in the same profile still strip."""
    html = (
        b'<html><body><h1>Byt</h1>'
        b'<div class="advertisement">ad</div></body></html>'
    )
    good = VolatileProfile(css_selectors=(".advertisement",))
    with_typo = VolatileProfile(
        css_selectors=("div..a", "", 'div[name="x', ">>", ".advertisement"),
    )

    clean = normalise(html, content_type=_HTML, volatile=good)
    typo = normalise(html, content_type=_HTML, volatile=with_typo)

    assert typo.norm_sha256 == clean.norm_sha256
    assert b"advertisement" not in typo.norm_bytes
    # ...and specifically NOT the raw fallback, which keeps the stripped node.
    assert typo.norm_sha256 != normalise(
        html, content_type=_HTML, volatile=_NONE,
    ).norm_sha256


def test_selector_is_usable_rejects_what_normalise_can_only_no_op() -> None:
    """The load-time gate: `normalise` is silent by contract, so a typo in a
    portal's volatile_paths can only ever no-op there. Callers that read those
    selectors out of the contract can refuse them loudly instead."""
    for malformed in ("div..a", "", 'div[name="x', "svg|circle", ">>"):
        assert selector_is_safe(malformed), malformed  # syntax is not its job
        assert not selector_is_usable(malformed), malformed

    assert not selector_is_usable('span.i-info__title:contains("Datum")')
    for good in ('input[name="tshee"]', "footer div.absolute.bottom-2.right-2",
                 "div.a:not(.b)", r".md\:flex"):
        assert selector_is_usable(good), good


def test_every_shipped_selector_is_usable_not_merely_safe() -> None:
    """If a future profile edit adds a selector the allowlist rejects — or one
    that is merely misspelt — it should fail loudly here rather than silently
    under-stripping in production (the churn readout would look better than
    reality). `usable` is the strict superset: safe AND parseable."""
    unusable = [
        (source, selector)
        for source, profile in DEFAULT_VOLATILE_PROFILES.items()
        for selector in profile.css_selectors
        if not selector_is_usable(selector)
    ]
    assert unusable == []


def test_idnes_measured_profile_strips_the_diff_probe_findings() -> None:
    """scripts/location_payload_diff_probe.py found these four moving on 5/5
    live listings across all 3 fetches; _FORM_TOKENS' input[name*="_token"]
    never covered them (schpeckc/tshee are not named "_token")."""
    html = (
        b'<html><body><h1>Byt 2+1</h1>'
        b'<form><input name="tshee" value="15237420345">'
        b'<input name="schpeckc" value="62da6c7c">'
        b'<span id="schpeckIn">3 \xe2\x9e\x95 6</span></form>'
        b'<div class="grid-similar-offers">other listings here</div>'
        b'</body></html>'
    )

    result = normalise(html, content_type=_HTML, volatile=DEFAULT_VOLATILE_PROFILES["idnes"])

    assert b"15237420345" not in result.norm_bytes
    assert b"62da6c7c" not in result.norm_bytes
    assert b"schpeckIn" not in result.norm_bytes
    assert b"grid-similar-offers" not in result.norm_bytes
    assert b"Byt 2+1" in result.norm_bytes


def test_ceskereality_measured_profile_strips_the_diff_probe_findings() -> None:
    html = (
        b'<html><body><h1>Byt 2+1</h1>'
        b'<input id="bug-report-token" value="8659c.5BgBggKSAVY6">'
        b'<section class="s-estates-slide">similar listings</section>'
        b'<div class="s-estate-detail-intro__slider">the subject gallery</div>'
        b'</body></html>'
    )

    result = normalise(
        html, content_type=_HTML, volatile=DEFAULT_VOLATILE_PROFILES["ceskereality"],
    )

    assert b"5BgBggKSAVY6" not in result.norm_bytes
    assert b"s-estates-slide" not in result.norm_bytes
    # the subject's own gallery is a sibling, not a match, and must survive
    assert b"the subject gallery" in result.norm_bytes


def test_realitymix_measured_profile_strips_the_diff_probe_findings() -> None:
    html = (
        b'<html><body><h1>Byt 2+1</h1>'
        b'<footer><div class="absolute bottom-2 right-2">0.85</div>'
        b'<div class="other-footer-content">keep me</div></footer>'
        b'</body></html>'
    )

    result = normalise(
        html, content_type=_HTML, volatile=DEFAULT_VOLATILE_PROFILES["realitymix"],
    )

    assert b"0.85" not in result.norm_bytes
    assert b"keep me" in result.norm_bytes


def test_normalizer_version_is_2_and_the_probe_cohort_follows_it() -> None:
    from location_data.payload_norm import probe_normalizer_version

    assert NORMALIZER_VERSION == "payload_norm@2"
    assert probe_normalizer_version() == "payload_norm@2+probe"
