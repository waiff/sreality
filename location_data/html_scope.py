"""The exclusion-zone scoper — D7's security boundary over an archived payload.

06-migration-backfill.md section 6.2.3: "Excluded-block scoping is mandatory here
too, and it is a security boundary (D7). Every portal ships a fully-formed
address-shaped decoy ... The deterministic HTML re-miner consumes the SAME
exclusion-zone register as the LLM lane (02 section 2.5), or it will import the
same contamination at scale." That scale is 445,191 archived pages, and the
contamination is not hypothetical: remax's neighbour-carousel `data-address`
already reached `listings.street` on 2 rows.

02-portal-contracts.md section 2.1.4 gives the register one shape and two
consumers — "the deterministic parser (a claim whose locator resolves inside an
excluded zone is rejected) and the LLM lane, where the scoped payload is built by
REMOVING these zones before the text ever reaches a model". This module is that
one artifact. Extraction never sees the raw body: `scope_html` returns a
`ScopedDocument` whose `.css()` is the only selector surface, so a decoy cannot be
reached by a selector that was never given the chance to match it.

THE REGISTER IS CONTRACT DATA, NEVER A PYTHON CONSTANT. Zones arrive as the
`{locator_kind, locator, reason}` list that `contracts.PortalContract.exclusion_zones`
parses out of `contracts/portals/<portal>.yaml` and `contracts.project()` writes to
`portal_contracts.exclusion_zones`. A selector hardcoded here would be a rule with
no `contract_version`, invisible to the fixture-diff gate and unretractable.

NOT EVERY ZONE IS A SUBTREE, and stripping the ones that are not would delete the
subject. idnes's neighbour zone is `script[data-maptiler-json]` qualified by
`then: /geojson/features[isSimilar=true]` — the same script carries the subject
Point that W2 exists to recover; mmreality's `[\\:property]` zone is qualified
`scope: non_subject_blobs` and the subject blob is one of them; ceskereality's
`offeredby.address` sits inside the JSON-LD script that also carries the
BreadcrumbList. So a zone is classified before it is applied: only an
UNQUALIFIED CSS zone is a DOM strip, and a qualified one is `deferred` — carried
on the result for the reader that CAN address a sub-document, never silently
dropped and never used to justify deleting the node.

`payload_scope_version` is what makes a scoping decision auditable after the fact:
it hashes the portal's whole register block plus SCOPER_VERSION, so a claim
written under one register can be told apart from the same claim written under
another, and re-verified against the bytes that produced it (01 section 4.2 makes
it NOT NULL on every evidence-bearing claim).

PURE — no DB, no network, no clock. It runs per listing inside a batch drain.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from selectolax.lexbor import LexborHTMLParser, LexborNode

SCOPER_VERSION = "html_scope@1"

# The reject rule 02 section 2.1.2 names and 02 section 2.1.4 defines. Registered
# here rather than in `claims_intake.GUARDS`, whose registry is typed as a
# coordinate predicate `(lat, lon) -> bool`; this one asks about a document.
GUARD_EXCLUDED_ZONE = "reject_if_in_excluded_zone"

# What can be DONE with a zone, which is not the same question as what it MEANS.
DOM = "dom"                  # an unqualified CSS subtree: removed before extraction
PAYLOAD = "payload"          # a sub-document exclusion; the JSON/text reader applies it
TEXT = "text"                # a description-pattern exclusion
UNSUPPORTED = "unsupported"  # addresses nothing this engine can act on

# lexbor, not the modest parser `payload_norm` uses: modest's CSS engine rejects
# `[\\:property]` / `[\\:locations]` outright ("Bad CSS Selectors"), which is
# mmreality's whole register. A zone the engine cannot compile is a hole in a
# security boundary, so the engine is chosen to fit the register rather than the
# register trimmed to fit the engine. Same `selectolax` package, no new dependency.
_PARSER = LexborHTMLParser

# A locator key that narrows the zone to part of what the CSS selects. Any one of
# them means the node is NOT the zone and must survive.
_SUBDOCUMENT_KEYS = frozenset({
    "then", "json_pointer", "scope", "type", "match", "pattern", "positions", "attr",
})

# `\s` already covers NBSP under Unicode; the zero-width space does not, and a
# scrubbed archive body carries both.
_WS_RE = re.compile("[\\s\u00a0\u200b]+")


class ScopeError(ValueError):
    """A register entry cannot be classified at all."""


@dataclass(frozen=True, slots=True)
class ExclusionZone:
    """One `{locator_kind, locator, reason}` register entry, classified."""

    index: int
    locator_kind: str
    disposition: str
    selector: str | None = None
    json_pointer: str | None = None
    pattern: str | None = None
    reason: str | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ScopeRegister:
    """A portal's exclusion zones plus the version stamp every claim carries."""

    source: str
    zones: tuple[ExclusionZone, ...]
    scope_version: str

    @classmethod
    def from_zones(
        cls, source: str, raw_zones: Sequence[Mapping[str, Any]] | None,
    ) -> ScopeRegister:
        zones = tuple(
            _classify(index, zone) for index, zone in enumerate(raw_zones or ())
        )
        return cls(
            source=source,
            zones=zones,
            scope_version=payload_scope_version(source, raw_zones),
        )

    @property
    def dom_selectors(self) -> tuple[str, ...]:
        return tuple(z.selector for z in self.zones if z.disposition == DOM and z.selector)

    @property
    def payload_pointers(self) -> tuple[str, ...]:
        return tuple(
            z.json_pointer for z in self.zones
            if z.disposition == PAYLOAD and z.json_pointer
        )

    @property
    def text_patterns(self) -> tuple[str, ...]:
        return tuple(z.pattern for z in self.zones if z.disposition == TEXT and z.pattern)

    @property
    def deferred(self) -> tuple[ExclusionZone, ...]:
        """Zones no DOM strip can honour — the reader that owns the sub-document must."""
        return tuple(z for z in self.zones if z.disposition in (PAYLOAD, TEXT))

    def text_admits(self, value: str) -> bool:
        return not any(
            re.search(pattern, value) for pattern in self.text_patterns
        )


def payload_scope_version(
    source: str, raw_zones: Sequence[Mapping[str, Any]] | None,
) -> str:
    """`location_claims.payload_scope_version` — the register block, hashed.

    Over the WHOLE block, not just the selectors: a claim has to resolve to the
    register bytes that scoped it, and a register edit is already a
    `contract_version` bump (02 section 2.1.8), so there is no such thing as a
    free change to a `reason`. Canonical JSON, so the YAML parse and the
    `portal_contracts.exclusion_zones` jsonb round-trip hash identically.
    """
    canonical = json.dumps(
        list(raw_zones or ()), sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), default=str,
    )
    digest = hashlib.sha256(
        "\x1f".join((SCOPER_VERSION, source, canonical)).encode("utf-8")
    ).hexdigest()
    return f"{SCOPER_VERSION}:{source}:{digest[:16]}"


def _classify(index: int, zone: Mapping[str, Any]) -> ExclusionZone:
    locator_kind = str(zone.get("locator_kind") or "")
    locator = zone.get("locator") or {}
    if not isinstance(locator, Mapping):
        raise ScopeError(f"zone #{index}: locator must be a mapping, got {type(locator)}")
    reason = zone.get("reason")
    css = locator.get("css")
    qualifiers = sorted(_SUBDOCUMENT_KEYS & set(locator))

    if css and not qualifiers:
        return ExclusionZone(
            index=index, locator_kind=locator_kind, disposition=DOM,
            selector=str(css), reason=reason)
    if css and qualifiers:
        return ExclusionZone(
            index=index, locator_kind=locator_kind, disposition=PAYLOAD,
            selector=str(css), json_pointer=_pointer_of(locator), reason=reason,
            note=(f"narrowed by {', '.join(qualifiers)}; the node carries subject data "
                  f"too and must survive the strip"))
    pointer = _pointer_of(locator)
    if pointer:
        return ExclusionZone(
            index=index, locator_kind=locator_kind, disposition=PAYLOAD,
            json_pointer=pointer, reason=reason,
            note="addresses the decoded payload, not the DOM")
    pattern = locator.get("pattern")
    if pattern:
        try:
            re.compile(str(pattern))
        except re.error as exc:
            # Classified, not raised: one bad pattern must not make the other
            # eight portals unscopeable, and CI asserts no zone is UNSUPPORTED.
            return ExclusionZone(
                index=index, locator_kind=locator_kind, disposition=UNSUPPORTED,
                pattern=str(pattern), reason=reason, note=f"uncompilable regex: {exc}")
        return ExclusionZone(
            index=index, locator_kind=locator_kind, disposition=TEXT,
            pattern=str(pattern), reason=reason)
    return ExclusionZone(
        index=index, locator_kind=locator_kind, disposition=UNSUPPORTED, reason=reason,
        note="no css, json_pointer or pattern — the zone addresses nothing executable")


def _pointer_of(locator: Mapping[str, Any]) -> str | None:
    pointer = locator.get("json_pointer") or locator.get("then")
    return str(pointer) if pointer else None


# ------------------------------------------------------------------ HTML scoping


@dataclass(frozen=True, slots=True)
class ScopedDocument:
    """What extraction is allowed to see, and the evidence of what was taken away.

    `html` is THE scoped payload: `span_start` / `span_end` on a claim are
    character offsets into it, which is what makes migration 382's "the quote is a
    substring of the SCOPED payload" check repeatable.
    """

    register: ScopeRegister
    html: str
    text: str
    removed_html: tuple[str, ...]
    nodes_removed: int
    unsupported_selectors: tuple[str, ...]
    parse_failed: bool
    strip_failures: int = 0
    _tree: Any = None

    @property
    def source(self) -> str:
        return self.register.source

    @property
    def scope_version(self) -> str:
        return self.register.scope_version

    @property
    def is_complete(self) -> bool:
        """False when a zone could not be applied — the boundary has a hole in it."""
        return (not self.parse_failed and not self.unsupported_selectors
                and not self.strip_failures)

    def css(self, selector: str) -> list[LexborNode]:
        """The ONLY selector surface. A stripped subtree is not in this tree."""
        if self._tree is None:
            return []
        try:
            return self._tree.css(selector)
        except Exception:
            return []

    def css_first(self, selector: str) -> LexborNode | None:
        nodes = self.css(selector)
        return nodes[0] if nodes else None

    def owns(self, node: LexborNode) -> bool:
        """`reject_if_in_excluded_zone` at node grain: did this node survive the strip?

        The literal form of the rule — a value whose source node sat inside an
        excluded subtree is refused — because a node that came out of THIS tree
        provably did not. A reader holding a node from its own parse of the raw
        body gets False, which is the point: the raw body is not the substrate.
        """
        return self._tree is not None and getattr(node, "parser", None) is self._tree

    def contains(self, value: str) -> bool:
        """Is `value` reachable in the scoped payload at all?

        Checked against the serialised HTML *and* the text: an attribute value
        (remax's `data-gps`) exists only in the former, and an entity-escaped one
        (`&amp;`) reads back only from the latter.
        """
        return _reachable(value, self.html, self.text)

    def admits(self, value: str) -> bool:
        return _admits(
            value, register=self.register, degraded=self.parse_failed,
            reachable=(self.html, self.text), removed=self.removed_html)

    def find_span(self, value: str) -> tuple[int, int] | None:
        """Character span of `value` in `html`, whitespace-tolerantly."""
        return _find_span(value, self.html)


def scope_html(
    body: bytes | str, *, register: ScopeRegister,
) -> ScopedDocument:
    """Parse once, strip every DOM zone, and hand back the only legal substrate.

    Fails CLOSED. A body that will not parse yields an EMPTY document rather than
    the raw one: on a security boundary, "the scoper broke" must not read as "no
    zones matched, extract freely".
    """
    try:
        tree = _PARSER(_decode(body))
    except Exception:
        return _failed_closed(register)

    matched: list[LexborNode] = []
    seen: set[int] = set()
    unsupported: list[str] = []
    for selector in register.dom_selectors:
        try:
            nodes = tree.css(selector)
        except Exception:
            unsupported.append(selector)
            continue
        for node in nodes:
            # Dedupe by identity BEFORE anything is freed: remax's zone selects the
            # same carousel card through `[data-address]` and through `[data-gps]`,
            # and decomposing one node twice corrupts the tree (observed: the second
            # decompose leaves the card's coordinates readable again in the
            # serialisation of the very document it was removed from).
            if node.mem_id in seen:
                continue
            seen.add(node.mem_id)
            matched.append(node)

    # Only the OUTERMOST match is removed. A nested zone would otherwise be freed
    # by its ancestor and then decomposed again — the same double-free, arrived at
    # through overlapping registers instead of overlapping selectors.
    outermost = [node for node in matched if not _has_matched_ancestor(node, seen)]
    removed: list[str] = []
    failures = 0
    for node in outermost:
        try:
            removed.append(node.html or "")
        except Exception:
            # Quoting what was removed is evidence, not the removal itself — the
            # strip below still has to happen or the zone stays in the tree.
            removed.append("")
        try:
            node.decompose()
        except Exception:
            failures += 1

    try:
        html, text = tree.html or "", tree.text() or ""
    except Exception:
        return _failed_closed(register)

    return ScopedDocument(
        register=register,
        html=html,
        text=text,
        removed_html=tuple(removed),
        nodes_removed=len(outermost) - failures,
        unsupported_selectors=tuple(unsupported),
        parse_failed=False,
        strip_failures=failures,
        _tree=tree,
    )


def _failed_closed(register: ScopeRegister) -> ScopedDocument:
    return ScopedDocument(
        register=register, html="", text="", removed_html=(), nodes_removed=0,
        unsupported_selectors=register.dom_selectors, parse_failed=True)


def _decode(body: bytes | str) -> str:
    """Always hand lexbor a `str`.

    `portal_raw_pages.html` is a TEXT column, so the runtime input already is one;
    bytes arrive only from a fixture or a fresh fetch. Parsing bytes that are not
    valid UTF-8 leaves lexbor holding them, and it is the SERIALISATION that then
    raises — after the strip, where a failure would be indistinguishable from a
    clean scope.
    """
    if isinstance(body, str):
        return body
    raw = bytes(body)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace")


def _has_matched_ancestor(node: LexborNode, matched_ids: set[int]) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.mem_id in matched_ids:
            return True
        parent = parent.parent
    return False


# ------------------------------------------------------------------ JSON scoping


@dataclass(frozen=True, slots=True)
class ScopedPayload:
    """The same boundary over a decoded JSON payload (sreality's `/premise`)."""

    register: ScopeRegister
    data: Any
    removed_pointers: tuple[str, ...]
    removed_json: tuple[str, ...]

    @property
    def source(self) -> str:
        return self.register.source

    @property
    def scope_version(self) -> str:
        return self.register.scope_version

    @property
    def text(self) -> str:
        return json.dumps(self.data, ensure_ascii=False, sort_keys=True, default=str)

    def contains(self, value: str) -> bool:
        return _reachable(value, self.text)

    def admits(self, value: str) -> bool:
        return _admits(
            value, register=self.register, degraded=False,
            reachable=(self.text,), removed=self.removed_json)


def scope_json(payload: Any, *, register: ScopeRegister) -> ScopedPayload:
    """Remove every `payload`-disposition pointer. The input is never mutated."""
    data = copy.deepcopy(payload)
    pointers: list[str] = []
    removed: list[str] = []
    for pointer in register.payload_pointers:
        popped = _pop_pointer(data, _pointer_tokens(pointer))
        if popped is _MISSING:
            continue
        pointers.append(pointer)
        removed.append(json.dumps(popped, ensure_ascii=False, sort_keys=True, default=str))
    return ScopedPayload(
        register=register, data=data,
        removed_pointers=tuple(pointers), removed_json=tuple(removed))


class _Missing:
    __slots__ = ()


_MISSING = _Missing()


def _pointer_tokens(pointer: str) -> tuple[str, ...]:
    """RFC 6901, and deliberately WITHOUT `payload_norm`'s wildcard extensions.

    A register pointer names one decoy subtree and the guard has to be able to
    quote what it removed; a glob delete would remove things no audit could
    enumerate afterwards.
    """
    if not pointer.startswith("/"):
        return ()
    return tuple(
        token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/")
    )


def _pop_pointer(node: Any, tokens: tuple[str, ...]) -> Any:
    if not tokens:
        return _MISSING
    head, rest = tokens[0], tokens[1:]
    if isinstance(node, dict):
        if head not in node:
            return _MISSING
        if rest:
            return _pop_pointer(node[head], rest)
        return node.pop(head)
    if isinstance(node, list) and head.isdigit() and int(head) < len(node):
        index = int(head)
        if rest:
            return _pop_pointer(node[index], rest)
        return node.pop(index)
    return _MISSING


# ------------------------------------------------------------------ the guard


def excluded_zone_admits(scoped: ScopedDocument | ScopedPayload, value: str) -> bool:
    """`reject_if_in_excluded_zone`, in `claims_intake.guard_admits` polarity.

    True admits. False means the value could only have come from a zone this
    register removes — the remax carousel street, the broker footer's PSČ, the
    `Zahraniční nemovitosti` nav item.
    """
    return scoped.admits(value)


def _admits(
    value: str,
    *,
    register: ScopeRegister,
    degraded: bool,
    reachable: tuple[str, ...],
    removed: Sequence[str],
) -> bool:
    if not value or not value.strip():
        return True
    if not register.text_admits(value):
        return False
    if degraded:
        # No scoped document exists, so nothing can be SHOWN to be outside a zone.
        return False
    if _reachable(value, *reachable):
        return True
    return not any(_reachable(value, fragment) for fragment in removed)


def _reachable(value: str, *documents: str) -> bool:
    needle = _collapse(value)
    if not needle:
        return False
    return any(needle in _collapse(document) for document in documents)


def _collapse(value: str) -> str:
    return _WS_RE.sub(" ", value).strip()


def _find_span(value: str, document: str) -> tuple[int, int] | None:
    if not value:
        return None
    start = document.find(value)
    if start >= 0:
        return start, start + len(value)
    # The quote came out of `node.text()`, whose whitespace runs no longer look
    # like the source's; the span still has to point at the source.
    parts = [re.escape(part) for part in value.split() if part]
    if not parts:
        return None
    match = re.search(r"\s+".join(parts), document)
    return (match.start(), match.end()) if match else None
