"""Deterministic payload normalisation for the W2a shadow-hash churn instrument.

02-portal-contracts.md section 2.3.2 P1 makes the append-on-change payload archive
content-addressed on a NORMALISED body, and turns the normaliser into a gate:
"fetch 200 listings x 3 fetches per portal, compute raw-vs-normalised change
rates, and only then set each contract's volatile_paths and enable P2". This
module is that normaliser, measured a week ahead of the cutover (06 section 6.9
OQ9) against live scrape traffic.

`normalise` is PURE — no network, no database, no clock, no randomness — so the
same bytes always hash the same way on any runner, and it NEVER raises: a body
the instrument cannot parse degrades to a raw-bytes fallback rather than killing
the scrape it is measuring.

DEFAULT_VOLATILE_PROFILES is a MEASUREMENT-PHASE artefact, not a contract. The
profiles that ship in portal_contract_entries.persistence.volatile_paths are
chosen FROM this measurement; these are the best guesses that make the first
readout informative. They are deliberately biased towards stripping: an
over-stripped profile understates raw-vs-norm separation (it makes the
normaliser look better than it is, and the next readout catches it), while an
under-stripped one silently inflates the change rate that the P2 storage
projection depends on.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from selectolax.parser import HTMLParser

NORMALIZER_VERSION = "payload_norm@1"

# The confirmation probe (02 section 2.3.2's 200 x 3 protocol,
# scripts/location_payload_refetch_probe.py) hashes with THIS normaliser but on a
# cadence of minutes, against the passive instrument's ~6 hours. Writing both into
# one counter row would wreck the passive readout twice over: the probe's three
# near-identical fetches would drag the measured change rate down, and its seconds
# would collapse the observed refetch interval ((last_seen_at - first_seen_at) /
# (fetches - 1)) that the storage projection scales by. `normalizer_version` is
# already the PK's cohort discriminator (migration 402), so the probe simply lands
# in its own cohort — same normaliser, stated, plus its provenance.
PROBE_NORMALIZER_SUFFIX = "+probe"


def probe_normalizer_version(version: str = NORMALIZER_VERSION) -> str:
    """The cohort key the confirmation probe writes under."""
    return f"{version}{PROBE_NORMALIZER_SUFFIX}"


# ASCII-only class on purpose: it must apply byte-wise to a body that failed to
# decode as UTF-8 (the degraded path) without inventing an encoding for it.
_WS_RE = re.compile(rb"[ \t\r\n\f\v]+")

# RFC 6901 has no wildcard (its `-` addresses the slot past the end of an array,
# which is meaningless for a delete). We reuse the token as "every element of
# this array" so a profile can strip the re-signed CDN URL out of every entry of
# sreality's advert_images without knowing how many photos a listing has.
_WILDCARD = "-"

# The second extension: a `*` inside an OBJECT-key token globs that key, so a
# profile can express scraper.hashing's prefix+suffix rules (sdn_*_attachment_url)
# instead of enumerating the members it happens to have seen. Exactly one `*` per
# token, matched as prefix + suffix; a literal `*` in a portal's key is
# unreachable, which is the same trade RFC 6901 already makes for `-`.
_KEY_GLOB = "*"


@dataclass(frozen=True)
class VolatileProfile:
    """What a source's body carries that changes without the listing changing."""

    json_pointers: tuple[str, ...] = ()
    css_selectors: tuple[str, ...] = ()
    strip_attributes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Normalised:
    norm_bytes: bytes
    norm_sha256: bytes
    raw_sha256: bytes
    byte_size: int
    norm_byte_size: int


def normalise(body: bytes, *, content_type: str, volatile: VolatileProfile) -> Normalised:
    """Hash `body` twice: as fetched, and as a volatile-stripped canonical form.

    JSON bodies canonicalise to sorted, separator-tight UTF-8; HTML bodies lose
    the profile's nodes and attributes and every whitespace run; anything else
    (and anything that fails to parse) falls back to the raw bytes with
    whitespace runs collapsed, which is the weakest normalisation that still
    cannot raise.
    """
    kind = _body_kind(content_type)
    norm: bytes | None = None
    if kind == "json":
        norm = _normalise_json(body, volatile.json_pointers)
    elif kind == "html":
        norm = _normalise_html(body, volatile.css_selectors, volatile.strip_attributes)
    if norm is None:
        norm = _collapse_ws(body)
    return Normalised(
        norm_bytes=norm,
        norm_sha256=hashlib.sha256(norm).digest(),
        raw_sha256=hashlib.sha256(body).digest(),
        byte_size=len(body),
        norm_byte_size=len(norm),
    )


def sniff_content_type(body: bytes) -> str:
    """Best-effort content type from the first non-blank byte.

    The archive path (scraper.db.upsert_portal_raw_page) is handed a `html`
    string by seven HTML portals AND by two JSON archivers, with no declared
    type — so the instrument sniffs rather than trusting the parameter name.
    """
    head = body[:512].lstrip(b"\xef\xbb\xbf \t\r\n\f\v")
    if head[:1] in (b"{", b"["):
        return "application/json"
    if head[:1] == b"<":
        return "text/html"
    return "application/octet-stream"


def _body_kind(content_type: str) -> str:
    lowered = content_type.lower()
    if "json" in lowered:
        return "json"
    if "html" in lowered or "xml" in lowered:
        return "html"
    return "other"


def _collapse_ws(body: bytes) -> bytes:
    return _WS_RE.sub(b" ", body).strip()


def _normalise_json(body: bytes, pointers: tuple[str, ...]) -> bytes | None:
    try:
        doc = json.loads(body.decode("utf-8"))
        for pointer in pointers:
            _delete_pointer(doc, _pointer_tokens(pointer))
        return json.dumps(
            doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
    except Exception:
        return None


def _normalise_html(
    body: bytes, selectors: tuple[str, ...], attributes: tuple[str, ...],
) -> bytes | None:
    try:
        tree = HTMLParser(body)
        for selector in selectors:
            for node in tree.css(selector):
                # A match nested inside an already-decomposed match is freed
                # memory; skipping it is cheaper than ordering the matches.
                try:
                    node.decompose()
                except Exception:
                    continue
        root = tree.root
        if attributes and root is not None:
            for node in root.traverse(include_text=False):
                for name in attributes:
                    if name in node.attributes:
                        del node.attrs[name]
        return _collapse_ws((tree.html or "").encode("utf-8"))
    except Exception:
        return None


def _pointer_tokens(pointer: str) -> tuple[str, ...]:
    if not pointer.startswith("/"):
        return ()
    return tuple(
        token.replace("~1", "/").replace("~0", "~")
        for token in pointer[1:].split("/")
    )


def _delete_pointer(node: Any, tokens: tuple[str, ...]) -> None:
    if not tokens:
        return
    head, rest = tokens[0], tokens[1:]
    if head == _WILDCARD and isinstance(node, list):
        if not rest:
            del node[:]
            return
        for item in node:
            _delete_pointer(item, rest)
        return
    if _KEY_GLOB in head and isinstance(node, dict):
        for key in _glob_keys(node, head):
            if rest:
                _delete_pointer(node[key], rest)
            else:
                node.pop(key, None)
        return
    if not rest:
        _drop(node, head)
        return
    _delete_pointer(_child(node, head), rest)


def _glob_keys(node: dict[str, Any], token: str) -> list[str]:
    prefix, _, suffix = token.partition(_KEY_GLOB)
    return [
        key for key in list(node)
        if isinstance(key, str)
        and len(key) >= len(prefix) + len(suffix)
        and key.startswith(prefix)
        and key.endswith(suffix)
    ]


def _child(node: Any, token: str) -> Any:
    if isinstance(node, dict):
        return node.get(token)
    if isinstance(node, list):
        index = _index(node, token)
        return None if index is None else node[index]
    return None


def _drop(node: Any, token: str) -> None:
    if isinstance(node, dict):
        node.pop(token, None)
        return
    if isinstance(node, list):
        index = _index(node, token)
        if index is not None:
            del node[index]


def _index(node: list[Any], token: str) -> int | None:
    if not token.isdigit():
        return None
    value = int(token)
    return value if value < len(node) else None


# --- measurement-phase volatile profiles (see the module docstring) ---

# Third-party ad / analytics loaders, matched by src so the portals' OWN inline
# <script> blocks survive: mmreality's whole payload is an embedded JSON script
# tag, and ceskereality/realitymix carry map configuration the same way, so
# stripping `script` wholesale would delete the location signal W2 exists to mine.
_ANALYTICS_SCRIPTS: tuple[str, ...] = (
    'script[src*="gemius"]',
    'script[src*="adform"]',
    'script[src*="googletagmanager"]',
    'script[src*="googletagservices"]',
    'script[src*="google-analytics"]',
    'script[src*="doubleclick"]',
    'script[src*="sklik"]',
    'script[src*="imedia.cz"]',
    'script[src*="connect.facebook.net"]',
    'script[src*="hotjar"]',
    'script[src*="smartlook"]',
)

# Presentation-only plumbing every HTML portal carries. `iframe` is deliberately
# ABSENT: an embedded map iframe carries coordinates in its src on several
# portals, which is exactly the artefact W2 extracts from.
_PAGE_CHROME: tuple[str, ...] = (
    "noscript",
    "style",
    'link[rel="preload"]',
    'link[rel="modulepreload"]',
    'link[rel="prefetch"]',
)

# Per-request CSRF material. Verified live in the remax detail fixture: Symfony
# hidden inputs named `<form>[_token]` whose value re-rolls on every fetch.
_FORM_TOKENS: tuple[str, ...] = (
    'input[name*="_token"]',
    'input[name*="csrf"]',
    'meta[name*="csrf"]',
    'meta[name*="token"]',
)

_HTML_BASE: tuple[str, ...] = _ANALYTICS_SCRIPTS + _PAGE_CHROME + _FORM_TOKENS

# Per-response attributes: CSP nonces and SRI digests re-roll per request/deploy
# and appear on nodes the parsers do read, so they are stripped in place rather
# than by removing the node.
_HTML_ATTRS: tuple[str, ...] = (
    "nonce",
    "data-nonce",
    "integrity",
    "data-csrf",
    "data-timestamp",
    "data-request-id",
    "data-requestid",
)

DEFAULT_VOLATILE_PROFILES: dict[str, VolatileProfile] = {
    # api_json. The pointers are the JSON form of scraper.hashing's already-proven
    # volatile key set (view counters, re-promotion dates, session/recommendation
    # blocks, the firmy.cz review counters) plus the re-signed sdn.cz media URLs,
    # which that module documents as "re-signs wholesale ... same image id,
    # different path" — the single largest source of sreality byte churn.
    # One member of that set is deliberately absent: hashing.py also drops the
    # `items` entry NAMED "Aktualizace", a value predicate a JSON pointer cannot
    # express. It is a timestamp item, so it inflates the measured rate slightly
    # — the safe direction (over-, not under-stating churn).
    "sreality": VolatileProfile(json_pointers=(
        "/stats",
        "/params/stats",  # legacy camelCase raw_json puts the view counter here
        "/edited",
        "/labels",
        "/labels_extended",
        "/is_topped",
        "/is_topped_today",
        "/logged_in",
        "/note",
        "/rus",
        "/rus_reply",
        "/user/image",
        "/premise/logo",
        "/premise/review_count",
        "/premise/review_score",
        "/premise/premise_paid_firmy",
        "/premise/company/sos_custom_advert_card",  # flips false<->true portal-side
        "/advert_images/-/url",
        "/advert_images/-/kind",
        "/advert_images/-/width",
        "/advert_images/-/height",
        "/videos/-/url",
        "/items/-/topped",
        # hashing.py's _ATTACHMENT_URL_PREFIX/_SUFFIX rule, as a key glob: the
        # energy-certificate PDFs re-sign the same way the image URLs do, and
        # enumerating today's members would miss tomorrow's.
        "/sdn_*_attachment_url",
        "/_embedded/favourite",
        "/_embedded/note",
    )),
    # graphql. The body is exactly the closed field list _DETAIL_QUERY asks for —
    # no ads, no tokens, no counters — so the null profile is the honest starting
    # guess and the readout will show whether the image URLs re-sign.
    "bezrealitky": VolatileProfile(),
    # html_selector. `div.inzeratyview` is the per-listing view counter the index
    # parser already reads ("Vidělo: N lidí"); it increments on every visit.
    "bazos": VolatileProfile(
        css_selectors=_HTML_BASE + ("div.inzeratyview",),
        strip_attributes=_HTML_ATTRS,
    ),
    # html_selector. `.advertisement` blocks are the stehuju.cz / vyklizim.cz
    # partner slots, verified in the idnes detail fixture.
    "idnes": VolatileProfile(
        css_selectors=_HTML_BASE + (".advertisement",),
        strip_attributes=_HTML_ATTRS,
    ),
    # embedded_json inside HTML — the inline <script> payload must survive, so
    # only the shared third-party/chrome/token set applies.
    "mmreality": VolatileProfile(
        css_selectors=_HTML_BASE,
        strip_attributes=_HTML_ATTRS,
    ),
    # html_selector. Symfony `[_token]` hidden inputs on three separate contact
    # forms, verified in the remax detail fixture.
    "remax": VolatileProfile(
        css_selectors=_HTML_BASE,
        strip_attributes=_HTML_ATTRS,
    ),
    "ceskereality": VolatileProfile(
        css_selectors=_HTML_BASE,
        strip_attributes=_HTML_ATTRS,
    ),
    # html_selector. Build-hashed asset filenames (`267f89b9.js`) re-roll on every
    # deploy; the preload/prefetch links that carry them are stripped by _PAGE_CHROME.
    "realitymix": VolatileProfile(
        css_selectors=_HTML_BASE,
        strip_attributes=_HTML_ATTRS,
    ),
    "maxima": VolatileProfile(
        css_selectors=_HTML_BASE,
        strip_attributes=_HTML_ATTRS,
    ),
}
