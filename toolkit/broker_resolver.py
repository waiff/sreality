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
than naive union-find): a pair auto-merges only with corroboration — ≥2 independent
bridges, or 1 bridge + a matching name — and only when BOTH sources are auto-merge-
enabled. Everything else (single weak bridge, name disagreement, oversized
component) is left for operator review. Connected components are formed over the
corroborated edges only, with a size cap, so one recycled phone number cannot
transitively fuse a chain of distinct people.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Sequence
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


def names_match(a: str | None, b: str | None) -> bool:
    ka, kb = name_key(a), name_key(b)
    return ka is not None and ka == kb


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
) -> MergeDecision:
    """Turn personal-contact bridges into corroborated auto-merge groups + review pairs.

    A pair is corroborated (auto-merge eligible) when, between the two identities,
    there are ≥2 distinct bridge values OR 1 bridge value plus a matching name —
    and both identities' sources are auto-merge-enabled. Components are built over
    corroborated edges only; an oversized component is downgraded entirely to review.
    """
    by_id = {i.id: i for i in identities}
    enabled = {s.lower() for s in auto_merge_sources}
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
        ia, ib = by_id[a], by_id[b]
        both_enabled = ia.source.lower() in enabled and ib.source.lower() in enabled
        strong = len(values) >= 2 or (len(values) >= 1 and names_match(ia.name, ib.name))
        if both_enabled and strong:
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
        decision.auto_merge_groups.append(sorted(members))

    # Stable, de-duplicated review output.
    decision.review_pairs = sorted({p if p[0] < p[1] else (p[1], p[0]) for p in decision.review_pairs})
    return decision
