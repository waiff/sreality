"""Pure cross-source broker identity-resolution rules (no I/O).

The orchestrator (`scripts.resolve_brokers`) does the SQL; this module holds the
deterministic, unit-tested logic for the one hard part — deciding which per-source
`broker_identities` are the same human across portals.

Keystone (validated against live data): a contact (email/phone) bridges identity
across sources ONLY if it is personal on BOTH sides — frequency==1 within each
source. Shared/role inboxes (`info@…` → hundreds of brokers) and toll-free/
switchboard numbers (one number → hundreds of brokers) are excluded as bridges
(the SQL frequency table enforces this; this module receives only personal
contacts). Within a source the portal-native id is authoritative and never merged.

Merging is conservative (mirrors the dedup engine's layered confirmation rather
than naive union-find) and the NAME is the deciding axis, not a tiebreaker:

  names agree  + ≥1 bridge  -> auto-merge (both sources auto-merge-enabled)
  names conflict outright   -> auto-dismiss; never shown to the operator
  anything else             -> operator review

Contact count alone no longer authorises a merge. The old bar took ≥2 shared
contacts as sufficient regardless of name, which merged demonstrably different
people whenever one broker's mobile appeared on a colleague's card. Conversely a
bridged pair whose names share no token at all is not a question worth asking.
"Conflict" is deliberately hard to reach — see `name_relation`. Connected
components are formed over the corroborated edges only, with a size cap, so one
recycled phone number cannot transitively fuse a chain of distinct people.

Operator decisions are durable: `suppressed_pairs` carries the identity pairs an
unmerge or a dismissal already rejected (broker_merge_suppressions, migration 401).
Without it the sweep re-derives the same bridges nightly and re-applies a merge the
operator undid.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Sequence, Set as AbstractSet
from dataclasses import dataclass, field

# A component larger than this is suspicious (a contact slipped the freq guard, or
# a dense recycled-number chain) — never auto-merge it; queue every pair instead.
MAX_AUTO_MERGE_COMPONENT = 6


@dataclass(frozen=True)
class Identity:
    """A per-source broker identity, as the resolver sees it for grouping."""

    id: int
    source: str
    name: str | None = None


@dataclass(frozen=True)
class Bridge:
    """A personal-on-both-sides contact shared by two cross-source identities."""

    left_id: int
    right_id: int
    kind: str  # 'email' | 'phone'
    value: str

    def pair(self) -> tuple[int, int]:
        return (self.left_id, self.right_id) if self.left_id < self.right_id else (self.right_id, self.left_id)


@dataclass
class MergeDecision:
    """The resolver's verdict for one run."""

    auto_merge_groups: list[list[int]] = field(default_factory=list)  # each = identity ids to unify
    review_pairs: list[tuple[int, int]] = field(default_factory=list)  # cross-source pairs for the operator
    suppressed: list[tuple[int, int]] = field(default_factory=list)  # pairs the operator already rejected
    dismiss_pairs: list[tuple[int, int]] = field(default_factory=list)  # bridged but names conflict
    # group tuple -> the single (kind, value) it traces to, when unambiguous.
    group_bridges: dict[tuple[int, ...], tuple[str, str]] = field(default_factory=dict)


def normalize_email(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip().lower()
    if "@" not in s or s.startswith("@") or s.endswith("@"):
        return None
    local, _, domain = s.partition("@")
    if not local or "." not in domain:
        return None
    return s


def email_domain(email: str | None) -> str | None:
    e = normalize_email(email)
    return e.split("@", 1)[1] if e else None


def normalize_phone(raw: str | None) -> str | None:
    """Digits-only, CZ-canonicalised: a bare 9-digit national number gains '420'."""
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 9:
        return "420" + digits
    if len(digits) < 9:
        return None
    return digits


def is_free_provider(domain: str | None, free_domains: Iterable[str]) -> bool:
    if not domain:
        return False
    return domain.lower() in {d.lower() for d in free_domains}


def name_key(name: str | None) -> str | None:
    """Order- and diacritics-insensitive key so 'Jan Novák' == 'Novák Jan'."""
    if not name:
        return None
    stripped = "".join(
        c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c)
    )
    tokens = sorted(t for t in "".join(
        ch if ch.isalnum() else " " for ch in stripped.lower()
    ).split() if t)
    return " ".join(tokens) or None


# Czech academic/professional titles carry no identity signal — the same human is
# "Jan Novák" on one portal and "Ing. Jan Novák" on another. Folding them away is
# what lets the comparison below be strict enough to auto-dismiss a real mismatch
# without also dismissing one person spelled with a degree.
_TITLE_TOKENS = frozenset({
    "bc", "bca", "ing", "arch", "mgr", "mga", "judr", "mudr", "mvdr", "phdr",
    "rndr", "pharmdr", "thdr", "paeddr", "dr", "phd", "csc", "drsc", "dis",
    "mba", "llm", "ll", "ph", "th", "doc", "prof", "akad", "et", "al",
})


def name_tokens(name: str | None) -> frozenset[str]:
    """Identity-bearing tokens only: diacritics folded, titles and bare initials dropped."""
    if not name:
        return frozenset()
    stripped = "".join(
        c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c)
    )
    return frozenset(
        t for t in "".join(
            ch if ch.isalnum() else " " for ch in stripped.lower()
        ).split()
        if len(t) > 1 and not t.isdigit() and t not in _TITLE_TOKENS
    )


def name_relation(a: str | None, b: str | None) -> str:
    """Three-valued name comparison: 'same' | 'different' | 'unknown'.

    'different' is the ONLY verdict that authorises an automatic dismissal, so it is
    deliberately the hardest to reach: both names must be present and share no
    identity-bearing token at all. A subset or partial overlap ('J. Novák' vs 'Jan
    Novák'), or a missing name, is 'unknown' — that stays an operator decision.
    """
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return "unknown"
    if ta == tb:
        return "same"
    return "different" if ta.isdisjoint(tb) else "unknown"


def names_match(a: str | None, b: str | None) -> bool:
    return name_relation(a, b) == "same"


def _union_find(node_ids: Iterable[int], edges: Iterable[tuple[int, int]]) -> dict[int, list[int]]:
    parent: dict[int, int] = {n: n for n in node_ids}

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    for a, b in edges:
        if a not in parent or b not in parent:
            continue
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[min(ra, rb)] = parent[max(ra, rb)] = min(ra, rb)

    groups: dict[int, list[int]] = {}
    for n in parent:
        groups.setdefault(find(n), []).append(n)
    return groups


def decide_merges(
    identities: Sequence[Identity],
    bridges: Sequence[Bridge],
    auto_merge_sources: Iterable[str],
    *,
    suppressed_pairs: AbstractSet[tuple[int, int]] | None = None,
) -> MergeDecision:
    """Turn personal-contact bridges into corroborated auto-merge groups + review pairs.

    A pair is corroborated (auto-merge eligible) when, between the two identities,
    there are ≥2 distinct bridge values OR 1 bridge value plus a matching name —
    and both identities' sources are auto-merge-enabled. Components are built over
    corroborated edges only; an oversized component is downgraded entirely to review.

    `suppressed_pairs` (normalized lo<hi identity ids, the same key `Bridge.pair()`
    produces) are decisions the operator already made — an undone merge or a
    dismissed review candidate. A suppressed pair reaches NEITHER the corroborated
    edges NOR the review queue: re-proposing it would ask the operator the same
    question every night, and the whole point of the rail is that the answer is on
    record. Lifting is an explicit operator merge, which the writer handles.
    """
    by_id = {i.id: i for i in identities}
    enabled = {s.lower() for s in auto_merge_sources}
    blocked = suppressed_pairs if suppressed_pairs is not None else frozenset()
    decision = MergeDecision()

    # Aggregate the distinct bridge values per unordered cross-source pair.
    per_pair: dict[tuple[int, int], set[str]] = {}
    for b in bridges:
        if b.left_id not in by_id or b.right_id not in by_id:
            continue
        if by_id[b.left_id].source == by_id[b.right_id].source:
            continue  # never merge within a source
        per_pair.setdefault(b.pair(), set()).add(f"{b.kind}:{b.value}")

    corroborated_edges: list[tuple[int, int]] = []
    for (a, b), values in per_pair.items():
        if (a, b) in blocked:
            decision.suppressed.append((a, b))
            continue
        ia, ib = by_id[a], by_id[b]
        both_enabled = ia.source.lower() in enabled and ib.source.lower() in enabled
        # The name is the deciding axis, not a tiebreaker. Two shared contacts used
        # to be enough on their own, which merged demonstrably different people
        # (one broker's mobile listed under a colleague's card bridges them twice).
        # Now: names agree -> merge; names conflict outright -> dismiss, never ask;
        # anything in between -> the operator.
        rel = name_relation(ia.name, ib.name)
        if rel == "different":
            decision.dismiss_pairs.append((a, b))
        elif both_enabled and rel == "same":
            corroborated_edges.append((a, b))
        else:
            decision.review_pairs.append((a, b))

    nodes = {n for edge in corroborated_edges for n in edge}
    for root, members in _union_find(nodes, corroborated_edges).items():
        if len(members) < 2:
            continue
        if len(members) > MAX_AUTO_MERGE_COMPONENT:
            # Too big to trust — queue every internal pair instead of auto-merging.
            ms = sorted(members)
            for i in range(len(ms)):
                for j in range(i + 1, len(ms)):
                    decision.review_pairs.append((ms[i], ms[j]))
            continue
        group = sorted(members)
        decision.auto_merge_groups.append(group)
        # The dominant case is a two-identity group over one edge carrying one
        # contact: name the bridge so broker_merge_events can record WHAT merged
        # them (NULL on all 7,689 rows written before this). Anything ambiguous
        # (a multi-edge component, an edge with two values) stays unstamped rather
        # than guessing which contact was decisive.
        edges_in = [e for e in corroborated_edges if e[0] in members and e[1] in members]
        if len(edges_in) == 1 and len(per_pair[edges_in[0]]) == 1:
            kind, _, value = next(iter(per_pair[edges_in[0]])).partition(":")
            decision.group_bridges[tuple(group)] = (kind, value)

    # Stable, de-duplicated review + suppression output. The blocked filter runs HERE,
    # not only on the per-pair edge loop: the oversized-component downgrade above
    # expands a component pairwise, and those transitive pairs never passed through
    # `per_pair`. Left unfiltered, an unmerge-origin suppression became a brand-new
    # review card every single sweep (no prior candidate row blocks it — the
    # status='proposed' guard only stops re-proposing a row that already exists) and
    # was counted in queued_for_review AND suppressed at the same time.
    review = {p if p[0] < p[1] else (p[1], p[0]) for p in decision.review_pairs}
    decision.review_pairs = sorted(p for p in review if p not in blocked)
    decision.suppressed = sorted(set(decision.suppressed) | {p for p in review if p in blocked})
    # Blocked pairs never reach the rel check (they `continue` above), so nothing
    # here can contradict a decision the operator already made.
    decision.dismiss_pairs = sorted(set(decision.dismiss_pairs))
    return decision
