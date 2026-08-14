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

THE PROFILES LIVE IN THE PORTAL CONTRACTS (W2a-3b), NOT IN THIS FILE.
`contracts/portals/<portal>.yaml` -> `persistence.volatile_paths.<page_kind>` is
their single home, so a change to what a portal strips is a reviewed diff on a
versioned, retractable artefact like every other extraction rule — not a Python
edit that ships with whatever else was in the branch. This module owns the
ALGORITHM and the portal-agnostic floor (`BASE_PROFILE`); it owns no portal's
rules. The measurement narrative for each one (which fetch differed, on how many
listings, and why the selector is the narrowest form that covers it) moved with
the values and is the comment block above each contract's `persistence:` key.

LIVING IN THE CONTRACT IS NOT THE SAME AS SHARING ITS VERSION. `persistence:` is
excluded from `contract_sha256` (`contracts.contract_body_hash`) and its profiles
are identified here by their own digest, because `contract_version` governs the
EXTRACTION entries — it is what `extractor_version` and `contract_entry_id` name,
and re-versioning those re-inserts the whole claims corpus (five million rows).
Archive configuration must not be able to spend that, and an extraction fix must
not be able to reset this instrument. One artefact, two identities, each covering
exactly what it governs.

WHY GIT AND NOT THE DB PROJECTION. 02 section 2.1.8 makes git the store of record
and `portal_contracts`/`portal_contract_entries` a deploy-time projection of it,
and `contracts.py` still projects `persistence` there for review in psql. But
`payload_sha256` is a PERMANENT content address that every evidence span inherits,
so the projection that produces it must be a function of the deployed artefact
ALONE. Read from the DB, it would additionally be a function of whether the
contract-load job had run yet — two runners hashing one body two ways, at the same
moment, recoverable only through the label. The contract files ship in the same
image as this code (Dockerfile `COPY contracts/`), so there is no such window.

A PROFILE BELONGS TO A (source, page_kind) PAIR, NOT TO A PORTAL. A detail page
is one property; an index page is a LIST of properties fetched on a walk cadence.
They are different documents that happen to share a hostname, and every profile
the fleet ships was derived by diffing DETAIL pages (W2a-3b/3c). Applying one to
an index body is applying a measurement to a population it was never taken from —
which is why the contract key carries the surface axis, and why a surface no
contract declares falls back to `BASE_PROFILE` rather than borrowing the portal's
detail rules.

The asymmetry that decides that fallback: on a MEASURED surface, over-stripping is
self-correcting — the residue diff shows what the profile ate, and the next readout
catches it. On an UNMEASURED surface it is not. A selector that deletes the whole
listing grid off an index page reports a 0% change rate, and 0% reads as the best
possible result; nothing downstream can tell "nothing changed" from "we deleted
everything and every page now hashes alike". So the bias towards stripping holds
only where a diff was actually run, and `BASE_PROFILE` carries only what is
portal-agnostic and content-free by construction (third-party analytics loaders
matched by src, page chrome, CSRF material, per-response attributes).

A SELECTOR NOW REACHES THE CSS ENGINE FROM YAML, so it is validated where refusing
is allowed — `contracts.py`'s parse and this module's own loader — never at normalise
time, which is silent by contract. `selector_is_usable` is that gate: `normalise` can
only ever no-op a malformed selector, and a no-op selector does not fail, it quietly
stops stripping and reports the residue as churn.
Every contract is parsed by the test suite (`contracts.load_all`), so a typo fails
`test.yml` on the push that introduces it, before the deploy-time `--load` ever sees it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from selectolax.parser import HTMLParser

# The contracts are the profiles' home, and they ship beside this code rather than
# being fetched from anywhere — see the module docstring.
CONTRACT_DIR = Path(__file__).resolve().parent.parent / "contracts" / "portals"

# THE ENGINE's version — the algorithm in this file, not any portal's rules. Those
# now live in the contracts and are identified by a digest of the declaration itself
# (`PROFILE_DIGEST_SUFFIX`), so the two axes that can move a normalised byte are
# versioned separately and both are named in the cohort label (see
# `resolve_normalisation`).
#
# NOT bumped by the move to contracts: the projections are byte-identical (proved
# fixture by fixture in tests/location_data/test_payload_norm_by_page_kind.py's pinned
# digests, which were computed under the code this replaces), and bumping an engine
# whose output did not move would discard the detail evidence accumulating under it
# for nothing.
#
# @3 (W2a-3c): mmreality and remax joined the measured set — both had measured 100%
# once the churn baseline finally accumulated repeat fetches for them.
# @2 (W2a-3b): the idnes / ceskereality / realitymix profiles stopped being guesses.
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


# A surface no contract declares is hashed by a DIFFERENT instrument than one that
# does — the shared base, not that portal's diffed rules — and `normalizer_version`
# is what a reader of portal_payload_churn has to tell them apart with. Migration
# 402's own header states the failure this avoids: relabelling a cohort in place
# "would blend @1-era counters into the @2 readout and register one phantom change
# per key on its first @2 fetch (the hash moved because the normaliser moved)".
#
# It maintains itself: the day an index profile is measured and declared, that
# surface stops being `+base` and opens its own clean cohort with no human
# remembering to bump anything. And it is contract-INDEPENDENT — the base is this
# module's own floor, byte-identical under every contract version — so the index
# cohorts accumulating today survive the move to contracts untouched.
BASE_PROFILE_SUFFIX = "+base"

# The other half of that pair: the contract DID declare a profile for this surface,
# and this names WHICH DECLARATION — as a digest of the resolved profile, not as the
# `contract_version` that happened to carry it.
#
# It is not the bare `payload_norm@N` and must never be: the values are no longer
# this module's, so a row stamped `payload_norm@3` would name an instrument (a table
# in this file) that no longer exists — migration 405 already reserved a
# non-`payload_norm@N` form for exactly this. Both axes are named because both can
# move a byte: the engine (@3) and the profile (its digest).
#
# WHY THE DIGEST AND NOT `contract_version`. A contract version moves for reasons that
# have nothing to do with normalisation — a locator fix, a new extraction entry, a
# closed coverage gap; ceskereality and realitymix each took two such bumps in the
# fortnight before this shipped. Keyed on the version, every one of those would land
# in portal_payload_churn's PK (migration 402), orphan that surface's accumulated
# counters and restart the readout at `fetches=1` — while the projection those counters
# measure had not moved a byte. That is exactly the waste NORMALIZER_VERSION's own
# comment above refuses on the engine axis ("bumping an engine whose output did not
# move would discard the detail evidence accumulating under it for nothing"); this is
# the same refusal on the profile axis. Digesting the profile makes the cohort break
# IFF the projection actually moves, in either direction: an edit to volatile_paths
# with no version bump still opens a clean cohort, which the version could not see.
#
# The portal is NOT in the label — `source` is already a column of both tables that
# carry it (portal_payload_churn's PK, portal_raw_payloads), and a second copy of a key
# is a thing that can disagree with the first. Two portals that declare the same rules
# therefore share a digest, which is honest: it is one instrument, and their rows are
# still told apart by `source`.
PROFILE_DIGEST_SUFFIX = "+profile@"

# 8 hex = 32 bits over a space of a few dozen profiles that will ever exist (nine
# portals x the handful of surfaces anyone diffs), so a collision — two DIFFERENT
# profiles labelled alike, which would silently blend two cohorts — is ~1e-8 territory
# and the label stays short enough to read in a psql row. The full digest is what the
# fixture gate pins (tests/location_data/test_volatile_paths_contract.py).
PROFILE_DIGEST_CHARS = 8

# `location_page_kind`'s labels (migration 380). They live HERE, not in
# `location_data.contracts`, because both loaders of a `volatile_paths` mapping have to
# check a declared key against them and this module is the one that must stay importable
# without the contract lane (the churn hook defers its import so a flag-off scrape never
# pays for location_data). `contracts.PAGE_KINDS` is this same object — one enum, two
# names, no chance of the CI gate and the runtime disagreeing about what a page_kind is.
PAGE_KINDS = frozenset({
    "index", "detail", "map", "gazetteer", "snapshot", "archive", "none",
})

# Only `detail` has ever been diffed, so it is the only one spelled out; scraper.db
# keeps its own copy of the same two string values because payload_norm must stay
# importable without pulling the scraper in (tests/location_data/test_payload_norm.py
# pins the pair together).
PAGE_KIND_DETAIL = "detail"

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
# This matters because these selectors are sourced from the contracts'
# persistence.volatile_paths — i.e. from outside this file, as YAML.
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
    ('div..a', an empty string from YAML) can only ever no-op there. The two
    places that LOAD those selectors — `parse_profile_block` here and
    `contracts.parse_contract`'s gate, which runs it on every CI `--check` —
    call this instead, where refusing loudly is allowed.
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


def profile_digest(profile: VolatileProfile) -> str:
    """A content address for the PROJECTION this profile produces — the cohort key.

    Over the RESOLVED profile (the declared rules concatenated onto their base), because
    that is what `normalise` is handed: two declarations that resolve to the same rules
    are the same instrument and belong in one cohort, and a base that moved under an
    unchanged declaration is a different instrument and does not.

    ORDER IS PART OF IT even though it cannot change the output bytes (every rule is
    applied). A reordered profile is a reviewed diff on the artefact, and a digest that
    ignored it would report "unchanged" about a file that changed — the digest is a
    provenance statement about a declaration, not only about its effect.
    """
    blob = json.dumps(
        [list(profile.json_pointers), list(profile.css_selectors),
         list(profile.strip_attributes)],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


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
        # selector ('div..a', '', 'div[name="x'), and a profile is sourced from a
        # contract's persistence.volatile_paths rather than from this file. The
        # contract gate refuses those at load time; this is the second rail, for a
        # body that reaches here anyway. Caught one level up, one typo would drop the whole
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


# --- the portal-agnostic floor a contract's `base:` selects (see the docstring) ---
#
# THIS is what stays in Python, and deliberately: it is not a portal fact. Every
# member is generic web plumbing that carries no listing content on ANY portal or
# surface, so it needs no per-portal measurement, no review and no retraction —
# which is exactly what makes the contract the right home for everything else and
# the wrong home for this.

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

# The floor for a (source, page_kind) no contract declares, and the one a contract
# block asks for with `base: html`. Every member is portal-agnostic AND content-free
# by construction, so it cannot delete a listing, an address or a map widget off a
# surface it was never measured against. Verified on live index bodies: on remax it
# matches the page's one <noscript> and one <style> (173 B of 209 KB), on
# ceskereality two <noscript>, one <style> and ten preload/modulepreload links
# (~1.3 KB of 180 KB).
#
# It is inert on a JSON surface by construction: `_normalise_json` reads only
# `json_pointers`, and this profile has none. sreality's index and bezrealitky's
# gazetteer therefore normalise byte-for-byte under it.
BASE_PROFILE = VolatileProfile(
    css_selectors=_HTML_BASE,
    strip_attributes=_HTML_ATTRS,
)

# `base: none` — no floor at all. The honest choice on a JSON surface, where the
# HTML floor would be inert anyway (sreality) or where the body is a closed field
# list with no chrome in it to strip (bezrealitky's GraphQL response).
_NO_BASE = VolatileProfile()

BASE_PROFILES: dict[str, VolatileProfile] = {"html": BASE_PROFILE, "none": _NO_BASE}


# --- the contract's declaration, parsed (02 section 2.3.2 P1) -----------------

class ProfileError(RuntimeError):
    """A contract's `persistence.volatile_paths` cannot be turned into a profile.

    Raised where refusing is allowed — contract parse, contract projection, and the
    first resolution in a process — never inside `normalise`, which is silent by
    contract and can only ever no-op a bad rule.
    """


# One page_kind block's keys. Unknown key is a REFUSAL for the same reason
# `contracts._TOP_LEVEL_KEYS` is: `css_selector:` misspelt once declares a profile
# that strips nothing, and a profile that strips nothing reports a change rate that
# is simply wrong rather than absent.
_PROFILE_BLOCK_KEYS = frozenset({
    "base", "json_pointers", "css_selectors", "strip_attributes",
})


def _string_list(block: dict[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = block.get(key) or []
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ProfileError(f"{where}: {key} must be a list of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ProfileError(f"{where}: {key} carries a non-string or empty entry")
        out.append(item)
    return tuple(out)


def parse_profile_block(block: Any, *, where: str) -> VolatileProfile:
    """One page_kind's declared block -> the profile `normalise` will be handed.

    EVERY rule is checked here, at contract-load time, because this is the last
    place a bad one can be refused: `normalise` skips an unusable selector silently
    (it must — `.css()` raises on a typo and `:contains()` SEGFAULTS the parser), so
    a mistake that reaches it does not fail, it QUIETLY STOPS STRIPPING. That reads
    downstream as a higher change rate on a real portal, i.e. as a measurement.
    """
    if not isinstance(block, dict):
        raise ProfileError(
            f"{where}: a page_kind's volatile paths are a mapping with a 'base' and "
            f"optional json_pointers / css_selectors / strip_attributes lists")
    unknown = sorted(set(block) - _PROFILE_BLOCK_KEYS)
    if unknown:
        raise ProfileError(
            f"{where}: unknown key(s) {', '.join(unknown)}; known keys are "
            + ", ".join(sorted(_PROFILE_BLOCK_KEYS)))

    base_name = block.get("base")
    if base_name not in BASE_PROFILES:
        raise ProfileError(
            f"{where}: base={base_name!r} must be one of "
            f"{', '.join(sorted(BASE_PROFILES))} — the floor is stated per surface "
            f"rather than defaulted, so no body ever acquires one by accident")
    base = BASE_PROFILES[str(base_name)]

    pointers = _string_list(block, "json_pointers", where)
    for pointer in pointers:
        # `_pointer_tokens` returns () for anything not rooted at '/', which deletes
        # nothing at all — the silent no-op again, one layer down from the selectors.
        if not pointer.startswith("/"):
            raise ProfileError(
                f"{where}: json_pointer {pointer!r} must start with '/' (RFC 6901); "
                f"anything else addresses nothing and would strip nothing")

    selectors = _string_list(block, "css_selectors", where)
    for selector in selectors:
        if not selector_is_usable(selector):
            raise ProfileError(
                f"{where}: css_selector {selector!r} is not usable — either it names a "
                f"pseudo-class selectolax does not implement (`:contains()` SEGFAULTS "
                f"the parser: exit 139, uncatchable) or it does not parse. `normalise` "
                f"would skip it in silence and under-strip this surface")

    attributes = _string_list(block, "strip_attributes", where)
    for name in attributes:
        if name != name.strip() or " " in name:
            raise ProfileError(
                f"{where}: strip_attribute {name!r} is an attribute NAME, matched "
                f"exactly against node.attributes — whitespace can never match one")

    # Concatenated base-first, which is the order the measured table shipped
    # (`_HTML_BASE + _IDNES_VOLATILE`). Order is irrelevant to the output bytes —
    # every rule is applied — but keeping it makes the projected profile compare
    # equal to the one it replaces, so "this move changed nothing" is a tuple
    # equality rather than an argument.
    return VolatileProfile(
        json_pointers=base.json_pointers + pointers,
        css_selectors=base.css_selectors + selectors,
        strip_attributes=base.strip_attributes + attributes,
    )


def parse_volatile_paths(
    declared: Any, *, where: str, page_kinds: frozenset[str] | None = PAGE_KINDS,
) -> dict[str, VolatileProfile]:
    """`persistence.volatile_paths` -> {page_kind: profile}.

    KEYED BY page_kind, never flat. A detail page is one property; an index page is a
    LIST of properties fetched on a walk cadence. They are different documents that
    happen to share a hostname, and every profile the fleet ships was derived by
    diffing DETAIL pages — so a flat list is a detail measurement silently applied to
    a population it was never taken from (fixed in Python by #1070; this is the same
    fix in the contract that now owns the values).

    `page_kinds` is migration 380's `location_page_kind` enum, defaulted to this
    module's `PAGE_KINDS` (which `contracts.PAGE_KINDS` re-exports, so the CI gate and
    the runtime cannot disagree). It defaults rather than being required because a
    typo'd page_kind is exactly the silent failure this validation exists for: the
    surface it was meant for keeps the base profile, the label stays honestly `+base`,
    and nothing anywhere reports that the declaration is dead. `None` disables the
    check, for a caller validating a mapping whose key space is not that enum.
    """
    if declared is None:
        return {}
    if isinstance(declared, list):
        raise ProfileError(
            f"{where}: volatile_paths is a MAPPING of page_kind -> profile, not a flat "
            f"list. A flat list applies a detail measurement to index bodies, which "
            f"are lists of other people's listings (W2a-3d)")
    if not isinstance(declared, dict):
        raise ProfileError(f"{where}: volatile_paths must be a mapping of page_kind -> profile")

    profiles: dict[str, VolatileProfile] = {}
    for page_kind, block in declared.items():
        kind = str(page_kind)
        if page_kinds is not None and kind not in page_kinds:
            raise ProfileError(
                f"{where}: volatile_paths names page_kind '{kind}', which is not a "
                f"location_page_kind label ({', '.join(sorted(page_kinds))}); the "
                f"surface it was meant for would silently get the base profile")
        profiles[kind] = parse_profile_block(block, where=f"{where}.{kind}")
    return profiles


@dataclass(frozen=True, slots=True)
class ContractProfiles:
    """Every portal's declared volatile profiles, as ONE immutable registry.

    `versions` is NOT the cohort key — the profile's own digest is (see
    `PROFILE_DIGEST_SUFFIX`) — but it is read from the same parse of the same file, so
    a reader who has a digest can be told which contract version currently declares it
    (`scripts/location_payload_churn_report.py` prints that map).
    """

    versions: dict[str, int]
    profiles: dict[tuple[str, str], VolatileProfile]

    def profile(self, source: str, page_kind: str) -> VolatileProfile | None:
        return self.profiles.get((source, page_kind))


def load_contract_profiles(directory: Path | None = None) -> ContractProfiles:
    """Read `persistence.volatile_paths` out of every portal contract on disk.

    Reads only `portal`, `contract_version` and `persistence` — NOT the extraction
    entries. The full contract gate (`location_data.contracts`) validates those, and
    it validates these through the very same parser; this narrow read is what keeps
    an unrelated entry problem from taking the normaliser down with it.

    RAISES rather than degrading. An empty or unreadable contract directory is a
    BUILD defect (the image ships `contracts/` beside the code), and the degradation
    it would otherwise cause is invisible: every portal silently falls to the base
    profile, and a base-profile change rate looks like a measurement.

    `directory` defaults at CALL time, not at import: bound as a default argument it
    reads `CONTRACT_DIR` once, and a test that monkeypatches the module attribute would
    silently keep loading the shipped contracts.
    """
    import yaml  # ships with the runtime image for exactly this read.

    root = Path(directory if directory is not None else CONTRACT_DIR)
    paths = sorted(root.glob("*.yaml"))
    if not paths:
        raise ProfileError(
            f"no portal contracts under {root} — the volatile profiles live "
            f"there (02 section 2.1.8: git is the store of record), so without them "
            f"nothing can be normalised under the projection it will be labelled with")

    versions: dict[str, int] = {}
    declared_by: dict[str, Path] = {}
    profiles: dict[tuple[str, str], VolatileProfile] = {}
    for path in paths:
        try:
            doc = yaml.safe_load(path.read_bytes().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - re-raised as this module's error
            raise ProfileError(f"{path}: unreadable contract ({exc})") from exc
        if not isinstance(doc, dict) or "portal" not in doc:
            raise ProfileError(f"{path}: not a portal contract")
        source = str(doc["portal"])
        # ONE file per portal. Two files naming one portal would be resolved key by
        # key in filename order — the version from the last file, each profile from
        # the last file that declared THAT page_kind — so a row could be stamped with
        # provenance from a contract that never supplied the rules it was normalised
        # under. That is the "a row claims an instrument it did not use" failure this
        # whole lane is built to prevent, and it is cheaper to refuse than to define.
        if source in declared_by:
            raise ProfileError(
                f"{path}: portal '{source}' is already declared by "
                f"{declared_by[source].name} — one contract file per portal, or the "
                f"registry pairs one file's rules with another file's provenance")
        declared_by[source] = path
        # Strict: a contract that cannot state its version is malformed, and the
        # registry is what tells an operator which contract version currently declares
        # the profile a cohort's digest names.
        try:
            version = int(doc["contract_version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProfileError(
                f"{path}: contract_version must be an integer — it is how a churn "
                f"cohort's profile digest is traced back to a reviewed artefact") from exc
        versions[source] = version
        declared = (doc.get("persistence") or {}).get("volatile_paths")
        for page_kind, profile in parse_volatile_paths(
            declared, where=f"{path.name}:persistence.volatile_paths",
            page_kinds=PAGE_KINDS,
        ).items():
            profiles[(source, page_kind)] = profile
    return ContractProfiles(versions=versions, profiles=profiles)


@lru_cache(maxsize=1)
def contract_profiles() -> ContractProfiles:
    """The loaded registry, once per process.

    Memoised because `resolve_normalisation` sits on the per-page path of both the
    churn instrument and the archive. Cached on SUCCESS only — `lru_cache` does not
    memoise an exception — so a transient read failure retries rather than pinning
    the fleet to a degraded answer. Tests that swap the contract directory call
    `contract_profiles.cache_clear()`.
    """
    return load_contract_profiles()


def volatile_profile(source: str, page_kind: str) -> VolatileProfile:
    """The profile this SURFACE's contract declares, or the generic base."""
    return resolve_normalisation(source, page_kind).profile


def normalizer_version_for(
    source: str, page_kind: str, version: str | None = None,
) -> str:
    """The cohort this (source, page_kind) fetch is counted under."""
    return resolve_normalisation(source, page_kind, version).normalizer_version


@dataclass(frozen=True, slots=True)
class Resolution:
    """What a surface is normalised under, and the label that names it — as ONE value.

    The two are answers to the same question, and asking them separately is what
    lets them disagree: a caller that takes the profile from one call and the stamp
    from another can write a `normalizer_version` naming an instrument that was not
    the one applied. `payload_sha256` is permanent and `normalizer_version` is the
    only thing that explains which projection produced it, so a disagreement is not
    a cosmetic label bug — it is a content address nobody can account for.
    """

    profile: VolatileProfile
    normalizer_version: str


def resolve_normalisation(
    source: str, page_kind: str, version: str | None = None,
) -> Resolution:
    """Profile AND cohort label for one surface. THE single resolution point.

    `scraper.db.record_payload_churn` (the live instrument), `payloads.append_payload`
    (the archive's content address) and `payload_backfill.encode_for_archive` (the
    445k-row migration) all come through here, so the two can never be resolved from
    two different surfaces — or from two different declarations.

    The label, and why it is not the bare `payload_norm@N` any more:

      `payload_norm@3+profile@4574b9ef`
                                   the contract declared a profile for this surface.
                                   Two axes move the output bytes and both are named:
                                   the ENGINE (this module's algorithm) and the
                                   PROFILE — as a digest of the rules themselves, so
                                   the cohort breaks IFF the projection moves, never
                                   because the file around the declaration was edited
                                   (see `PROFILE_DIGEST_SUFFIX`). The portal is NOT
                                   repeated into the label — `source` is already a
                                   column of both tables that carry it
                                   (portal_payload_churn's PK, portal_raw_payloads),
                                   and a second copy is a thing that can disagree.
      `payload_norm@3+base`        the contract declares nothing for this surface, so
                                   BASE_PROFILE was applied. Unchanged by the move to
                                   contracts, and honestly so: the base is a property
                                   of this module, identical under every contract
                                   version, so the index cohorts accumulating today
                                   keep accumulating across it.

    `version=None` rather than a `NORMALIZER_VERSION` DEFAULT ARGUMENT: a default is
    bound once at import, so a bump monkeypatched onto the module (the cohort-
    separation test in tests/test_payload_churn_live.py, which is how the "a
    normaliser bump opens a clean cohort" property is proved against real SQL) would
    be read here as the old value and the test would pass while measuring nothing.

    RAISES `ProfileError` if the contracts cannot be read at all. Both live callers
    are inside never-raising wrappers (`record_payload_churn_if_enabled`,
    `append_payload_if_enabled`), so the instrument and the archive go quiet and warn
    while the scrape is untouched; the backfill is a script and should die. That is
    the whole degradation contract, and it is a refusal rather than a fallback
    because the alternative — quietly normalising under the base and stamping
    `+base` — writes a PERMANENT content address under a projection nobody chose.
    """
    registry = contract_profiles()
    base = version if version is not None else NORMALIZER_VERSION
    profile = registry.profile(source, page_kind)
    if profile is None:
        return Resolution(BASE_PROFILE, f"{base}{BASE_PROFILE_SUFFIX}")
    # Digested from the profile ITSELF, so the label is a function of the one thing
    # that decides the bytes — nothing about the file it was declared in, nothing
    # about the registry's bookkeeping, can reach it.
    return Resolution(
        profile,
        f"{base}{PROFILE_DIGEST_SUFFIX}"
        f"{profile_digest(profile)[:PROFILE_DIGEST_CHARS]}")
