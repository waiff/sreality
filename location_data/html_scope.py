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

FAIL CLOSED IS THE WHOLE POINT. Every state in which a declared zone was NOT
applied — a body that would not parse, a selector the engine will not compile, a
`decompose()` that raised, a narrowing expression no pop can honour — makes the
result `is_complete = False`, and an incomplete result ADMITS NOTHING. "The
scoper broke" must never read as "no zones matched, extract freely". The one
state that is deliberately NOT incompleteness is a zone that compiled and matched
zero nodes: a page may legitimately not carry the block. That is counted instead
(`zone_matches` / `zones_unmatched`) so the re-mine lane can alarm on a zone that
matches nothing across a whole corpus, which is a register bug, not a page fact.

DECODED VALUES ARE COMPARED WITH DECODED VALUES. A claim's value arrives the way
a reader read it — `node.text()` and `node.attributes[...]` both come back with
entities resolved — so reachability is tested against a decoded projection of the
document (its text plus every attribute value), never against the serialisation.
Comparing a decoded value to re-escaped markup is how a boundary silently opens:
every remax coordinate contains a `"`, which the serialisation spells `&quot;`.

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
_WS_SPLIT_RE = re.compile("([\\s\u00a0\u200b]+)")

# The serialisation an evidence span indexes into still carries entities, and the
# quote it has to match came back decoded. One span search bridges the two.
_ENTITY_FORMS: dict[str, tuple[str, ...]] = {
    "&": ("&amp;", "&#38;"),
    "<": ("&lt;", "&#60;"),
    ">": ("&gt;", "&#62;"),
    "\"": ("&quot;", "&#34;"),
    "'": ("&#39;", "&apos;", "&#x27;"),
}
_WS_SPAN_SOURCE = "(?:[\\s\u00a0\u200b]|&nbsp;|&#160;|&#xa0;)+"


class ScopeError(ValueError):
    """A register entry cannot be classified, or a selector cannot be compiled."""


@dataclass(frozen=True, slots=True)
class ExclusionZone:
    """One `{locator_kind, locator, reason}` register entry, classified."""

    index: int
    locator_kind: str
    disposition: str
    selector: str | None = None
    json_pointer: str | None = None
    narrowing: str | None = None
    pattern: str | None = None
    reason: str | None = None
    note: str | None = None

    @property
    def label(self) -> str:
        """How this zone names itself in a completeness report."""
        return self.json_pointer or self.narrowing or self.selector or f"zone #{self.index}"


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
        """Only the zones a pop can actually honour — RFC 6901 and nothing else."""
        return tuple(
            z.json_pointer for z in self.zones
            if z.disposition == PAYLOAD and z.json_pointer
        )

    @property
    def unhonourable_payload_zones(self) -> tuple[ExclusionZone, ...]:
        """Payload zones narrowed below what a pointer can address.

        idnes's `then: /geojson/features[isSimilar=true]` is a predicate, not a
        pointer, and mmreality's `scope: non_subject_blobs` is a rule about which
        blob is the subject. Neither can be executed by `scope_json`, so a payload
        scoped by a register carrying one is INCOMPLETE and admits nothing — the
        reader that can address the sub-document has to apply it first.
        """
        return tuple(
            z for z in self.zones if z.disposition == PAYLOAD and not z.json_pointer
        )

    @property
    def narrowings(self) -> tuple[str, ...]:
        return tuple(z.narrowing for z in self.zones if z.narrowing)

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
        pointer = _pointer_of(locator)
        return ExclusionZone(
            index=index, locator_kind=locator_kind, disposition=PAYLOAD,
            selector=str(css), json_pointer=pointer,
            narrowing=None if pointer else _narrowing_of(locator, qualifiers),
            reason=reason,
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
    """The `then`/`json_pointer` value, but ONLY when a pop could honour it.

    `/geojson/features[isSimilar=true]` is a predicate wearing a pointer's clothes.
    Classifying it as poppable is what let `scope_json` return a payload that had
    silently kept its decoys while reporting a clean scope.
    """
    pointer = locator.get("json_pointer") or locator.get("then")
    if not pointer:
        return None
    text = str(pointer)
    return text if _is_rfc6901(text) else None


def _is_rfc6901(pointer: str) -> bool:
    return pointer.startswith("/") and "[" not in pointer and "]" not in pointer


def _narrowing_of(locator: Mapping[str, Any], qualifiers: Sequence[str]) -> str:
    """A stable label for a narrowing this engine cannot execute."""
    for key in qualifiers:
        value = locator.get(key)
        if value:
            return f"{key}={value}"
    return ",".join(qualifiers)


# ------------------------------------------------------------------ HTML scoping


@dataclass(frozen=True, slots=True)
class ScopedDocument:
    """What extraction is allowed to see, and the evidence of what was taken away.

    `html` is THE scoped payload: `span_start` / `span_end` on a claim are
    character offsets into it, which is what makes migration 382's "the quote is a
    substring of the SCOPED payload" check repeatable. It is the payload, not the
    search index — reachability runs over `_haystacks`, the decoded projection.
    """

    register: ScopeRegister
    html: str
    text: str
    removed_html: tuple[str, ...]
    nodes_removed: int
    unsupported_selectors: tuple[str, ...]
    parse_failed: bool
    strip_failures: int = 0
    zone_matches: tuple[tuple[str, int], ...] = ()
    _tree: Any = None
    _haystacks: tuple[str, ...] = ()
    _removed_haystacks: tuple[str, ...] = ()

    @property
    def source(self) -> str:
        return self.register.source

    @property
    def scope_version(self) -> str:
        return self.register.scope_version

    @property
    def is_complete(self) -> bool:
        """False when a zone could not be APPLIED — the boundary has a hole in it.

        A zone that compiled and matched nothing is not a hole: plenty of pages do
        not carry the block. That case is `zones_unmatched`, an alarm for the
        re-mine lane, not a reason to refuse this page's claims.
        """
        return (not self.parse_failed and not self.unsupported_selectors
                and not self.strip_failures)

    @property
    def zones_unmatched(self) -> tuple[str, ...]:
        """Declared zone selectors that matched NOTHING on this page.

        One page proves nothing; a whole corpus does. "Declared zone, 0 matches
        across N pages" is a register bug — the selector names markup the portal
        does not emit — and it is the metric that finds one without a human
        re-reading nine registers against nine archived pages. A selector that
        would not COMPILE is not counted here; that one is already a hole
        (`unsupported_selectors`), not a metric.
        """
        return tuple(selector for selector, count in self.zone_matches if count == 0)

    def css(self, selector: str) -> list[LexborNode]:
        """The ONLY selector surface. A stripped subtree is not in this tree.

        An uncompilable EXTRACTION selector raises. Swallowing it into `[]` would
        read downstream as "the field is absent on this page" when what happened
        is "the extractor is broken", and the fixture-diff gate cannot tell those
        two apart. A REGISTER selector is the opposite call — that one is recorded
        in `unsupported_selectors` so one bad zone cannot make a page unscopeable,
        and it already forces the guard closed.
        """
        if self._tree is None:
            return []
        try:
            return self._tree.css(selector)
        except Exception as exc:
            raise ScopeError(f"selector will not compile: {selector!r} ({exc})") from exc

    def css_first(self, selector: str) -> LexborNode | None:
        nodes = self.css(selector)
        return nodes[0] if nodes else None

    def owns(self, node: LexborNode) -> bool:
        """Did this node come out of the scoped tree?

        Narrow by construction, and deliberately so: every node a reader can
        obtain from `css()` is owned, because a node inside a stripped subtree no
        longer exists to be handed back. What `owns()` refuses is a node from a
        SEPARATE parse of the raw body — the substitution that would put the decoy
        back within reach. The strip is the enforcement; this is the assertion
        that a reader did not go around it.
        """
        return self._tree is not None and getattr(node, "parser", None) is self._tree

    def contains(self, value: str) -> bool:
        """Is `value` reachable in the scoped payload at all?

        Tested against the DECODED projection — the document's text plus every
        surviving attribute value — because that is the form a reader's value
        arrives in. remax's `data-gps` is `50°03'46.7"N,...`; the serialisation
        spells that `&quot;` and would never match itself.
        """
        return _in_any(_collapse(value), self._haystacks)

    def admits(self, value: str) -> bool:
        return _admits(
            value, register=self.register, degraded=not self.is_complete,
            reachable=self._haystacks, removed=self._removed_haystacks)

    def find_span(
        self, value: str, *, within: LexborNode | None = None,
    ) -> tuple[int, int] | None:
        """Character span of `value` in `html`, whitespace- and entity-tolerantly.

        `within` anchors the search to one node's own serialisation, so a quote
        that also occurs in the `<title>` gets the span of the node the claim was
        actually read from. An anchored miss is None, never a document-wide
        second guess: a span pointing at the wrong occurrence still satisfies
        382's substring check and is therefore worse than no span at all. Two
        byte-identical siblings still resolve to the first — the anchor narrows
        the search, it does not carry an offset lexbor never exposed.
        """
        if within is None:
            return _find_span(value, self.html)
        fragment = getattr(within, "html", None) or ""
        offset = self.html.find(fragment) if fragment else -1
        if offset < 0:
            return None
        span = _find_span(value, fragment)
        return (offset + span[0], offset + span[1]) if span else None


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
    zone_matches: list[tuple[str, int]] = []
    for selector in register.dom_selectors:
        try:
            nodes = tree.css(selector)
        except Exception:
            unsupported.append(selector)
            continue
        zone_matches.extend(_component_counts(tree, selector, len(nodes)))
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
    removed_haystacks: list[str] = []
    failures = 0
    for node in outermost:
        try:
            removed.append(node.html or "")
        except Exception:
            # Quoting what was removed is evidence, not the removal itself — the
            # strip below still has to happen or the zone stays in the tree.
            removed.append("")
        # Harvested while the subtree still exists, and DECODED: the guard has to
        # compare a reader's value with the same value as the reader would read it.
        removed_haystacks.extend(_node_haystacks(node))
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
        zone_matches=tuple(zone_matches),
        _tree=tree,
        _haystacks=_document_haystacks(tree, text),
        _removed_haystacks=tuple(removed_haystacks),
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


def _component_counts(
    tree: Any, selector: str, whole: int,
) -> tuple[tuple[str, int], ...]:
    """Per-component match counts for one declared zone selector.

    A selector LIST is a union of zones, and `.b-similar, .broker, nav` matching
    twice hides that `.broker` matched zero — which is exactly the register hole
    the counter exists to surface. Reported at the register's own grain whenever
    the split cannot be proved safe.
    """
    components = _selector_components(selector)
    if len(components) < 2:
        return ((selector, whole),)
    counts: list[tuple[str, int]] = []
    for component in components:
        try:
            counts.append((component, len(tree.css(component))))
        except Exception:
            return ((selector, whole),)
    return tuple(counts)


def _selector_components(selector: str) -> tuple[str, ...]:
    """Split a selector list at TOP-LEVEL commas; `:is(a, b)` stays one component."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote = ""
    escaped = False
    for char in selector:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            current.append(char)
            escaped = True
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    stripped = tuple(part.strip() for part in parts if part.strip())
    return stripped or (selector,)


# ------------------------------------------------------------------ JSON scoping


@dataclass(frozen=True, slots=True)
class ScopedPayload:
    """The same boundary over a decoded JSON payload (sreality's `/premise`)."""

    register: ScopeRegister
    data: Any
    removed_pointers: tuple[str, ...]
    removed_json: tuple[str, ...]
    unsupported_pointers: tuple[str, ...] = ()
    _haystacks: tuple[str, ...] = ()
    _removed_haystacks: tuple[str, ...] = ()

    @property
    def source(self) -> str:
        return self.register.source

    @property
    def scope_version(self) -> str:
        return self.register.scope_version

    @property
    def is_complete(self) -> bool:
        """False when the register declares a zone no pop can honour.

        A pointer that is absent from THIS payload is complete — there was nothing
        to remove. A pointer that is not a pointer at all (idnes's
        `/geojson/features[isSimilar=true]`, mmreality's `non_subject_blobs`) is a
        declared zone still standing, and the guard must not pretend otherwise.
        """
        return not self.unsupported_pointers

    @property
    def text(self) -> str:
        return json.dumps(self.data, ensure_ascii=False, sort_keys=True, default=str)

    def contains(self, value: str) -> bool:
        return _in_any(_collapse(value), self._haystacks)

    def admits(self, value: str) -> bool:
        return _admits(
            value, register=self.register, degraded=not self.is_complete,
            reachable=self._haystacks, removed=self._removed_haystacks)


def scope_json(payload: Any, *, register: ScopeRegister) -> ScopedPayload:
    """Remove every honourable pointer; report the ones no pop can. Never mutates."""
    data = copy.deepcopy(payload)
    pointers: list[str] = []
    removed: list[str] = []
    removed_haystacks: list[str] = []
    for pointer in register.payload_pointers:
        popped = _pop_pointer(data, _pointer_tokens(pointer))
        if popped is _MISSING:
            continue
        pointers.append(pointer)
        removed.append(json.dumps(popped, ensure_ascii=False, sort_keys=True, default=str))
        removed_haystacks.extend(_payload_haystacks(popped))
    return ScopedPayload(
        register=register, data=data,
        removed_pointers=tuple(pointers), removed_json=tuple(removed),
        unsupported_pointers=tuple(
            zone.label for zone in register.unhonourable_payload_zones),
        _haystacks=_payload_haystacks(data),
        _removed_haystacks=tuple(removed_haystacks))


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
    `Zahraniční nemovitosti` nav item — or that the boundary could not be fully
    applied, in which case nothing is admitted at all.
    """
    return scoped.admits(value)


def _admits(
    value: str,
    *,
    register: ScopeRegister,
    degraded: bool,
    reachable: Sequence[str],
    removed: Sequence[str],
) -> bool:
    """`reachable` / `removed` are ALREADY-COLLAPSED haystacks (see `_collapse`)."""
    if not value or not value.strip():
        return True
    if not register.text_admits(value):
        return False
    if degraded:
        # A zone that was not applied is a zone still standing, so nothing in this
        # document can be SHOWN to be outside one.
        return False
    needle = _collapse(value)
    if not needle:
        return True
    if _in_any(needle, reachable):
        return True
    return not _in_any(needle, removed)


def _in_any(needle: str, haystacks: Sequence[str]) -> bool:
    return bool(needle) and any(needle in haystack for haystack in haystacks)


def collapse_ws(value: str) -> str:
    """ONE whitespace normalisation for every consumer of a scoped document.

    Public because the archive lane's presence readers have to compare a contract literal
    against page text the portal broke across source lines. `str.split()` is the tempting
    substitute and it is wrong on this substrate: it does not know the zero-width space a
    scrubbed archive body carries, so a second implementation would disagree with the
    scoper's own matching on exactly the pages where it matters."""
    return _WS_RE.sub(" ", value).strip()


_collapse = collapse_ws


def _document_haystacks(tree: Any, text: str) -> tuple[str, ...]:
    """Everything a reader could DECODE out of the surviving tree.

    Collapsed once here rather than per guard call: a batch drain asks ~25 claims
    per page about a 90k-character document, and re-normalising both on every
    question cost ~2 ms per claim over the whole archive.
    """
    return tuple(
        _collapse(value)
        for value in (text, *_attribute_values(getattr(tree, "root", None)))
        if value
    )


def _node_haystacks(node: LexborNode) -> tuple[str, ...]:
    """The same decoded projection over a subtree about to be removed."""
    try:
        text = node.text() or ""
    except Exception:
        text = ""
    return tuple(
        _collapse(value) for value in (text, *_attribute_values(node)) if value
    )


def _attribute_values(node: Any) -> list[str]:
    if node is None:
        return []
    values: list[str] = []
    try:
        for element in node.traverse(include_text=False):
            for value in (element.attributes or {}).values():
                if value:
                    values.append(value)
    except Exception:
        return values
    return values


def _payload_haystacks(data: Any) -> tuple[str, ...]:
    """The serialised payload plus every string it carries, decoded.

    The dump alone would miss a value spelled with a `"` (JSON escapes it) and the
    scalars alone would miss a number a claim quotes as text; both are cheap.
    """
    dumped = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return tuple(_collapse(value) for value in (dumped, *_json_strings(data)) if value)


def _json_strings(data: Any) -> list[str]:
    strings: list[str] = []
    stack: list[Any] = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            strings.append(item)
        elif isinstance(item, Mapping):
            for key, value in item.items():
                if isinstance(key, str):
                    strings.append(key)
                stack.append(value)
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return strings


def _find_span(value: str, document: str) -> tuple[int, int] | None:
    if not value or not document:
        return None
    start = document.find(value)
    if start >= 0:
        return start, start + len(value)
    # The quote came out of `node.text()`, so its whitespace runs no longer look
    # like the source's and its `&`, `"` and NBSP came back decoded; the span
    # still has to point at the source.
    match = re.search(_span_pattern(value), document)
    return (match.start(), match.end()) if match else None


def _span_pattern(value: str) -> str:
    parts: list[str] = []
    for run in _WS_SPLIT_RE.split(value):
        if not run:
            continue
        parts.append(
            _WS_SPAN_SOURCE if _WS_RE.fullmatch(run)
            else "".join(_char_pattern(char) for char in run)
        )
    return "".join(parts)


def _char_pattern(char: str) -> str:
    forms = _ENTITY_FORMS.get(char)
    if not forms:
        return re.escape(char)
    return "(?:" + "|".join(re.escape(form) for form in (char, *forms)) + ")"
