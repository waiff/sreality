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

# @3 (W2a-3c): mmreality and remax joined them — both had measured 100% once the churn
# baseline finally accumulated repeat fetches for them.
# @2 (W2a-3b): the idnes / ceskereality / realitymix profiles stopped being guesses.
# Bumping is not bookkeeping — `normalizer_version` is part of portal_payload_churn's
# primary key (migration 402) precisely so a profile change opens a CLEAN cohort. Left
# at @2, the 100%-change rows measured under the old guesses would average together
# with the new ones and the storage projection would be signed off a blend of two
# different instruments.
NORMALIZER_VERSION = "payload_norm@3"

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

# selectolax's CSS engine SEGFAULTS on some pseudo-classes it does not implement,
# and only against a real document: `span.i-info__title:contains("Datum")` exits
# 139 deterministically on a live ceskereality detail page (selectolax 0.4.10) while
# returning a clean empty match on a three-node one. A segfault is not an exception
# — `normalise`'s "never raises" contract cannot catch it and the scrape process
# dies with it — so pseudo-classes are ALLOWLISTED rather than denylisted. Only
# these five are verified against a full-size page; anything else makes its selector
# a no-op, which under-strips (the safe direction) instead of crashing.
# This matters because W2a's next step sources these selectors from
# portal_contract_entries.persistence.volatile_paths — i.e. from outside this file.
_PSEUDO_RE = re.compile(r"::?([A-Za-z-]+)")
_SAFE_PSEUDO: frozenset[str] = frozenset({
    "not", "has", "first-child", "last-child", "nth-child",
})
# A pseudo-class colon never appears inside an attribute-value bracket, but a
# portal's own href/src VALUE can carry one (`a[href^="mailto:info"]`,
# `a[href*="tel:"]`) — matched literally, `:info`/`:tel` reads as an unlisted
# pseudo-class and the whole selector goes unsafe -> silent no-op -> the exact
# under-strip direction this module's docstring calls out as the dangerous one.
# Masking `[...]` spans before scanning removes that false positive.
_BRACKETED_RE = re.compile(r"\[[^\]]*\]")
# The same false positive, one level down: a CSS-ESCAPED colon is part of a class
# NAME, not a pseudo-class. Tailwind's responsive variants are written that way
# (`.md\:flex`, `div.lg\:hidden`) and selectolax matches them fine, so reading
# the `\:` as a pseudo-class would silently drop a legitimate selector — and
# realitymix, whose measured volatile node is already a Tailwind utility stack,
# is the portal most likely to need one next.
_ESCAPED_RE = re.compile(r"\\.", re.DOTALL)


def selector_is_safe(selector: str) -> bool:
    """Whether this selector's pseudo-classes are ones selectolax survives.

    Syntax-only, and deliberately so: it is the guard against the segfault that
    `normalise` cannot catch. A selector that is merely MALFORMED still passes
    here and is no-opped by `_normalise_html`'s per-selector except; use
    `selector_is_usable` where a reject can be logged.
    """
    scanned = _ESCAPED_RE.sub("", _BRACKETED_RE.sub("", selector))
    return all(name in _SAFE_PSEUDO for name in _PSEUDO_RE.findall(scanned))


def selector_is_usable(selector: str) -> bool:
    """Safe AND parseable by selectolax — the gate for contract-load validation.

    `normalise` is silent by contract, so a typo in a portal's volatile_paths
    ('div..a', an empty string from YAML) can only ever no-op there. Callers that
    LOAD those selectors (W2a's next step reads them from
    portal_contract_entries.persistence.volatile_paths) can call this at load
    time, where refusing loudly is allowed.
    """
    if not selector_is_safe(selector):
        return False
    try:
        HTMLParser(b"<html></html>").css(selector)
    except Exception:
        return False
    return True


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
    except Exception:
        return None
    for selector in selectors:
        if not selector_is_safe(selector):
            continue
        # PER-SELECTOR, not around the loop: `.css()` RAISES on a malformed
        # selector ('div..a', '', 'div[name="x'), and a profile is about to be
        # sourced from portal_contract_entries.persistence.volatile_paths rather
        # than from this file. Caught one level up, one typo would drop the whole
        # body to the raw-bytes fallback — that portal's measured change rate
        # jumps to ~100% and the storage projection is signed off a corrupt
        # number, silently, because `normalise` never raises. Skipping just the
        # bad selector under-strips by exactly one rule instead, the same
        # degradation an unsafe selector already gets.
        try:
            nodes = tree.css(selector)
        except Exception:
            continue
        for node in nodes:
            # A match nested inside an already-decomposed match is freed
            # memory; skipping it is cheaper than ordering the matches.
            try:
                node.decompose()
            except Exception:
                continue
    try:
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

# --- measured (W2a-3b), not guessed ---
#
# scripts/location_payload_diff_probe.py fetched the same live detail page three
# times ~10s apart on 5 listings per portal and structurally diffed the results.
# Nothing about a listing can change in ten seconds, so everything below was SEEN
# to move. Selectors are the narrowest form that covers the observed node — an
# over-broad one would delete real content and make two different listings hash
# alike, which is far worse than a change rate that is a few points too high.

# iDNES: 3/3 fetches differed on 5/5 listings. All of it is the Nette contact form
# ("Napište makléři") re-arming its anti-spam material per response, plus the
# recommendation rail.
#   input[name=tshee]     15237420345 -> 15237420357  (a per-response counter)
#   input[name=schpeckc]  62da6c7c... -> c26841cc...   (32-hex captcha answer hash)
#   #schpeckIn            "3 ➕ 6" -> "1 ➕ 1"          (the captcha question)
#   .grid-similar-offers  a whole different set of "Podobné nabídky" cards
# The form's `_token_` was already covered by _FORM_TOKENS' input[name*="_token"];
# these four are what it missed. The similar-offers rail holds OTHER listings'
# titles, prices, streets and thumbnails — never this listing's, so its rotation
# is pure archive churn (and mis-attributed location text if it stayed).
_IDNES_VOLATILE: tuple[str, ...] = (
    'input[name="tshee"]',
    'input[name="schpeckc"]',
    "#schpeckIn",
    "div.grid-similar-offers",
    ".advertisement",  # stehuju.cz / vyklizim.cz partner slots (fixture-verified)
)

# ceskereality: 3/3 fetches differed on 5/5 listings, and on 4 of the 5 the ONLY
# thing that differed was one hidden token in the "nahlásit chybu" modal —
#   input#bug-report-token  8659c.5BgBggKSAVY6... -> e25206d84de2b24c8.cM7EOua...
# It is `name="token"`, not `_token`, which is exactly why _FORM_TOKENS'
# input[name*="_token"] never caught it; the id is pinned rather than widening that
# shared rule to every portal on one portal's evidence.
# The 5th listing additionally rotated `section.s-estates-slide` ("Podobné
# nemovitosti"). That section is a sibling of the gallery, not part of it: the
# listing's own photos live under .s-estate-detail-intro__slider and are untouched,
# and the Google-Maps <iframe> carrying q=50.7277,15.5971 is untouched too.
_CESKEREALITY_VOLATILE: tuple[str, ...] = (
    "input#bug-report-token",
    "section.s-estates-slide",
)

# realitymix: 3/3 fetches differed on 5/5 listings, and across all 15 fetches the
# ONE thing that ever differed was a small badge pinned to the footer, cycling
# "0.85" / "0.84" / "101.85" — a per-response backend/version stamp. Nothing else
# moved at all: no ad slot, no token, no carousel.
# Those three values at their observed frequencies predict a 1 - sum(p^2) = 63%
# chance that any two consecutive fetches disagree, against the 66% this portal
# actually measured in production. The badge is the whole 66%.
# Scoped under the single <footer> because the Tailwind utility classes it is
# built from carry no meaning on their own.
_REALITYMIX_VOLATILE: tuple[str, ...] = (
    "footer div.absolute.bottom-2.right-2",
)

# --- measured (W2a-3c) ---
#
# Same probe, one axis wider. remax answers three fetches EIGHT SECONDS apart with
# byte-identical bodies and still measured 100% in production: its churn is minted
# per HTTP SESSION, not per response, and the live drain is a fresh process every
# run. `--fresh-session-per-round` (now the probe's default) is what made it visible;
# both portals below were measured that way, 5 listings x 3 rounds.

# mmreality: 3/3 fetches differed on 5/5 listings, and every byte of it is
# CLOUDFLARE EMAIL OBFUSCATION. The edge rewrites each mailto: on the way out into
#   <a href="/cdn-cgi/l/email-protection#325b5c545d725f5f4057535e5b464b1c5148">
#   <span class="__cf_email__" data-cfemail="325b5c545d72...">[email protected]</span>
# where the payload is the address XOR'd with a ONE-BYTE KEY THAT IS RANDOM PER
# RESPONSE — the leading octet. Both samples above decode to the same
# `info@mmreality.cz` under their own keys (0x32, 0x98); the ciphertext shares not one
# byte with itself between two fetches. That is a 100% change rate generated by a
# feature whose entire output is a re-encoding of a constant, which is the purest
# possible case of "churn carrying zero information".
# Only 2 anchors and 1 span per page, all inside section.rds-footer-contacts (the
# corporate switchboard address, not the agent's). The anchor is stripped whole
# because the ciphertext lives in its href FRAGMENT and no attribute-level rule can
# reach a fragment; the span is listed separately so an obfuscated address rendered
# outside such an anchor is covered too.
# Also seen, and already covered: input#contact__token on the agent contact form
# (`name="contact[_token]"`, so _FORM_TOKENS catches it) — a per-SESSION Symfony
# token, invisible until the fresh-session axis existed.
_MMREALITY_VOLATILE: tuple[str, ...] = (
    'a[href^="/cdn-cgi/l/email-protection"]',
    "span.__cf_email__",
)

# remax: byte-identical WITHIN a session (3 fetches, 8s apart, 5/5 listings), and
# 5/5 different across sessions. Three things move between sessions, all of them
# Symfony CSRF tokens, and two were already covered as DOM nodes:
#   input#dalten_web_listing_contact_form__token  aTyaPqHx... -> qQPbSWpB...
#   input#mortgage_contact_form__token            QZeUG3B7... -> LxR9SieY...
# The third is the one that made the portal measure 100%, and it is a shape no CSS
# selector can ever reach: the share widget's Bootstrap popover carries an ENTIRE
# ESCAPED <form> inside its `data-content` ATTRIBUTE, and that form has a `_token`
# input of its own —
#   button[data-content]  "...name='dalten_web_send_listing_form[_token]'
#                           value='Ayv0ioqxsf55sr66ldIruP6c0hRwiR_pu4vN4CS3Pn0'..."
# It is a STRING, not a node, so `input[name*="_token"]` cannot see it however wide
# the rule is widened. Stripping the two popover buttons is the narrowest expressible
# form (VolatileProfile has no "this attribute on that selector"); both are chrome —
# a Facebook/Twitter share pair and a mail-to-a-friend form — and the listing's URL
# they interpolate survives in the canonical link and og:url.
_REMAX_VOLATILE: tuple[str, ...] = (
    "div.pd-share__buttons button[data-content]",
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
    # html_selector. Measured: see _IDNES_VOLATILE.
    "idnes": VolatileProfile(
        css_selectors=_HTML_BASE + _IDNES_VOLATILE,
        strip_attributes=_HTML_ATTRS,
    ),
    # embedded_json inside HTML — the inline <script> payload must survive, so the
    # shared set stays src-scoped. Measured: see _MMREALITY_VOLATILE.
    "mmreality": VolatileProfile(
        css_selectors=_HTML_BASE + _MMREALITY_VOLATILE,
        strip_attributes=_HTML_ATTRS,
    ),
    # html_selector. Symfony `[_token]` hidden inputs on three separate contact
    # forms, verified in the remax detail fixture. Measured: see _REMAX_VOLATILE.
    "remax": VolatileProfile(
        css_selectors=_HTML_BASE + _REMAX_VOLATILE,
        strip_attributes=_HTML_ATTRS,
    ),
    # html_selector, proxied. Measured: see _CESKEREALITY_VOLATILE.
    "ceskereality": VolatileProfile(
        css_selectors=_HTML_BASE + _CESKEREALITY_VOLATILE,
        strip_attributes=_HTML_ATTRS,
    ),
    # html_selector. Measured: see _REALITYMIX_VOLATILE. Build-hashed asset
    # filenames (`267f89b9.js`) re-roll on every deploy; the preload/prefetch links
    # that carry them are stripped by _PAGE_CHROME.
    "realitymix": VolatileProfile(
        css_selectors=_HTML_BASE + _REALITYMIX_VOLATILE,
        strip_attributes=_HTML_ATTRS,
    ),
    "maxima": VolatileProfile(
        css_selectors=_HTML_BASE,
        strip_attributes=_HTML_ATTRS,
    ),
}
