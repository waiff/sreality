"""Pure broker identity-resolution rules (no I/O) — portal-agnostic.

The orchestrator (`scripts.resolve_brokers`) does the SQL; this module holds the
deterministic, unit-tested logic for the one hard part — deciding which
`broker_identities` rows are the same human. The portal is NOT part of that
decision: two records on ONE portal are as mergeable as two across portals
(the schema only ever required `UNIQUE(source, source_broker_id_native)`; the
old never-merge-within-a-source bar was policy, and it left the biggest duplicate
fans — six records of one agent on one portal — permanently apart).

The whole rule:

    MERGE two identities when their NAMES MATCH
      AND ( A: they share a DISCRIMINATING contact
            OR
            B: they share a firm AND that name appears at only ONE firm corpus-wide )

**Names match** is `name_key` equality — diacritics folded, token order ignored,
academic titles stripped (`Bc. Ondřej Kadlec` ≡ `Kadlec Ondřej`). No name, no edge.

**Discriminating contact** (path A): a (kind, value) whose carriers — across the
ENTIRE corpus, every source, every identity including those of already-merged
brokers — all carry the same single non-null `name_key`. This replaces the old
frequency==1 "personal contact" guard, which duplication defeated: six copies of
one agent made his own personal e-mail look shared (n=6) and the guard dropped the
one contact that proved they were the same person. Under the discrimination test
duplication REINFORCES the signal instead of destroying it, and role inboxes
(`info@…` under 353 different names) and switchboard numbers fail it exactly as
they did before — by carrying many names, not by carrying many rows.

**Firm rarity** (path B) substitutes market-wide name rarity for contact evidence:
a name that appears at no OTHER firm is very unlikely to be two people, so two
records of it at that firm are one person even with no contact in common (the
role-inbox-only shape: everyone reachable at the same switchboard). Common names
(`Jan Novák`, present at dozens of firms) and generic role labels (`Zákaznická
linka`) fail it automatically and stay in manual review.

The claim it rests on is narrower than "the name appears nowhere else", and three
guards keep it honest. The firm is the identity's OWN (its e-mail domain), so a
merge cannot collapse the very spread that proved a name spans two firms; an
identity that publishes no firm abstains from the spread rather than voting, so
independent brokers with no firm at all neither help nor block it. A FRANCHISE
firm is refused outright: `re-max.cz` is one brand over ~95 independent offices,
so two agents of one name there are not colleagues and their shared firm_id proves
nothing. And a cohort whose members carry disconfirming contacts — each a
discriminating one of the same kind, no value in common — is refused whole: that
is positive evidence of different people, and it is what a firm-branded display
name ("PREXIMA nemovitosti s.r.o." on five agents) otherwise walks straight past,
since a label that IS the firm's name is unique to that firm by construction.

The paths are OR'd, and the firm-spread test guards B ONLY — it is B's substitute
for contact evidence, not an extra bar on A. A shared discriminating contact merges
a common-named pair even if that name exists at fifty firms.

Everything the rule cannot prove stays with the operator: a same-name pair sharing
a NON-discriminating contact at two different firms becomes a review pair (the
same-firm shape is already the `name_firm` candidate tab — one card, not two).

Operator decisions outrank the rule in both directions. `suppressed_pairs` carries
the identity pairs an unmerge or a dismissal already rejected
(`broker_merge_suppressions`, migration 401): a suppressed pair reaches neither the
auto-merge groups nor the review queue, and a suppression anywhere inside a
component downgrades that whole component to review.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Sequence, Set as AbstractSet
from dataclasses import dataclass, field

# Every edge is name-gated and name_key equality is transitive, so a component of
# THIS layer is single-named by construction — the cross-name chain fusion the old
# cap of 6 guarded against (one recycled phone number chaining distinct people)
# cannot form here. (The apply layer can still chain two differently-named groups
# through a broker that already holds an identity in each; that is a merge already
# recorded, and `scripts.resolve_brokers._apply_merges` bounds it by this same
# number.) What the cap guards here is a role-account mega-pool: one switchboard or
# one role inbox under a single generic label (a live example carries 464 records),
# which is discriminating by the letter of the test and is not one human. 20 clears
# the largest observed genuine duplicate fan (7) with margin while stopping a
# hundred-record pool from fusing in one night.
#
# It doubles as the review-expansion ceiling. A downgraded component is expanded
# pairwise, which is n(n-1)/2 — 107,416 cards for that 464-record pool, every
# sweep, all of them sharing the one switchboard that chained it (so no downstream
# filter thins them). Past the cap the component is queued as its real EDGES
# instead: n-1 genuine same-name shared-contact pairs the operator can still judge,
# and never a one-click merge of two agents joined only several hops away.
MAX_AUTO_MERGE_COMPONENT = 20

# broker_merge_events.reason for an auto-merge, by the evidence that formed it.
# Free text in the schema (no CHECK) — these three strings are the convention.
REASON_CONTACT_NAME = "contact_name"
REASON_NAME_FIRM = "name_firm"
# Canonical order for a combined reason, so 'contact_name+name_firm' is the ONE
# spelling in the ledger (the orchestrator joins the same way for a chained merge).
REASON_ORDER = (REASON_CONTACT_NAME, REASON_NAME_FIRM)


# slots: the sweep instantiates one per identity and per contact row over the whole
# corpus (hundreds of thousands), where a per-instance __dict__ is the difference
# between a comfortable job and an OOM.
@dataclass(frozen=True, slots=True)
class Identity:
    """A per-source broker identity, as the resolver sees it for grouping.

    `mergeable=False` marks an identity whose broker is already merged away: it
    still COUNTS for the discrimination and name→firms maps (its name is evidence
    about who a contact belongs to) but never gets an edge of its own.

    `firm_id` is the identity's OWN firm (the one behind its e-mail domain), never
    a broker-level rollup: path B measures how many firms a name appears at, and a
    rollup collapses every identity of an already-merged broker onto one firm, so
    the very merge that proved a name spans two firms would erase that evidence on
    the next sweep. `franchise` marks a firm that is one brand over many
    independent offices (`firms.is_franchise`) — a shared firm_id there is not a
    shared employer, so path B does not run on it. `primary_firm_id` is the
    broker-level rollup and is used for ONE thing: not double-carding a pair the
    `name_firm` candidate generator (which groups on exactly that column) already
    proposes.
    """

    id: int
    source: str
    name: str | None = None
    firm_id: int | None = None
    mergeable: bool = True
    franchise: bool = False
    primary_firm_id: int | None = None


@dataclass(frozen=True, slots=True)
class Contact:
    """One (identity, kind, value) row of `broker_identity_contacts`."""

    identity_id: int
    kind: str  # 'email' | 'phone'
    value: str


@dataclass
class MergeDecision:
    """The resolver's verdict for one run."""

    auto_merge_groups: list[list[int]] = field(default_factory=list)  # each = identity ids to unify
    review_pairs: list[tuple[int, int]] = field(default_factory=list)  # pairs for the operator
    suppressed: list[tuple[int, int]] = field(default_factory=list)  # pairs the operator already rejected
    # Retained mechanism (PR #1096): pairs whose names conflict outright, retired
    # as candidate cards under resolved_by='auto:name_conflict'. The rule above
    # never PROPOSES a cross-name pair, so the engine no longer produces these —
    # the writer stays wired so the historical cohort keeps its retirement path.
    dismiss_pairs: list[tuple[int, int]] = field(default_factory=list)
    # group tuple -> the single (kind, value) it traces to, when unambiguous.
    group_bridges: dict[tuple[int, ...], tuple[str, str]] = field(default_factory=dict)
    # group tuple -> which evidence path formed it: 'contact_name' | 'name_firm' |
    # 'contact_name+name_firm'. Stamped into broker_merge_events.reason.
    group_reasons: dict[tuple[int, ...], str] = field(default_factory=dict)


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


# Czech academic/professional titles carry no identity signal — the same human is
# "Jan Novák" on one portal and "Ing. Jan Novák" on another. The name gate is
# strict equality, so a title left in the key is a merge silently not made.
_TITLE_TOKENS = frozenset({
    "bc", "bca", "ing", "arch", "mgr", "mga", "judr", "mudr", "mddr", "mvdr",
    "phdr", "rndr", "pharmdr", "thlic", "thdr", "paeddr", "dr", "phd", "csc",
    "drsc", "dis", "mba", "llm", "msc", "prof", "doc",
})

# `name_tokens` (the three-valued comparison) additionally folds the fragments a
# tokenizer leaves behind when a title is written with dots inside a larger string.
_NOISE_TOKENS = _TITLE_TOKENS | {"ll", "ph", "th", "akad", "et", "al"}


def _identity_tokens(name: str) -> list[str]:
    """Diacritics-folded, lower-cased name tokens with title tokens removed.

    Whitespace chunks are tested for titlehood with their punctuation squashed
    OUT ('Ph.D.' -> 'phd'), so a dotted degree is dropped whole instead of leaving
    a stray 'd' behind; anything that is not a title is then split on punctuation
    as before, so a hyphenated surname stays two tokens.
    """
    folded = "".join(
        c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c)
    ).lower()
    out: list[str] = []
    for chunk in folded.replace(",", " ").replace(";", " ").split():
        if "".join(ch for ch in chunk if ch.isalnum()) in _TITLE_TOKENS:
            continue
        out.extend(
            t for t in "".join(ch if ch.isalnum() else " " for ch in chunk).split()
            if t and t not in _TITLE_TOKENS
        )
    return out


def name_key(name: str | None) -> str | None:
    """The name gate: order-, diacritics- and title-insensitive.

    'Jan Novák' == 'Novák Jan' == 'Ing. Jan Novák'. A string that is nothing but
    titles ('Ing.') has no identity content at all and keys to None, which never
    matches anything — including another title-only string.
    """
    if not name:
        return None
    return " ".join(sorted(_identity_tokens(name))) or None


def name_tokens(name: str | None) -> frozenset[str]:
    """Identity-bearing tokens only: titles, noise fragments and bare initials dropped."""
    if not name:
        return frozenset()
    return frozenset(
        t for t in _identity_tokens(name)
        if len(t) > 1 and not t.isdigit() and t not in _NOISE_TOKENS
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
    """The merge gate itself: equal keys, not merely overlapping tokens."""
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


def _pairs(members: Sequence[int]) -> list[tuple[int, int]]:
    return [(members[i], members[j])
            for i in range(len(members)) for j in range(i + 1, len(members))]


def _contradicted(members: Sequence[int],
                  discriminating: dict[int, dict[str, set[str]]]) -> bool:
    """True when these identities carry disconfirming contact evidence.

    Path B merges on the ABSENCE of contradicting evidence, and absence is what a
    missing contact looks like too. Two identities that each hold a discriminating
    contact of the same kind, with no value in common, are positive evidence of two
    different people — the shape of a generic display name that IS the firm's name
    (five agents at one agency all published as "PREXIMA nemovitosti s.r.o.", each
    with a personal mailbox: unique to its firm by construction, so the rarity test
    can never catch it). The whole (name, firm) cohort is refused rather than the
    offending pair, so the answer does not depend on chain order; it lands in the
    `name_firm` candidate tab, which exists for exactly this cohort.
    """
    by_kind: dict[str, list[set[str]]] = {}
    for member in members:
        for kind, values in discriminating.get(member, {}).items():
            by_kind.setdefault(kind, []).append(values)
    return any(len(sets) > 1 and not set.intersection(*sets) for sets in by_kind.values())


def decide_merges(
    identities: Sequence[Identity],
    contacts: Sequence[Contact],
    *,
    suppressed_pairs: AbstractSet[tuple[int, int]] | None = None,
) -> MergeDecision:
    """Apply the name-gated rule to a whole corpus snapshot.

    `identities` is EVERY identity (including those of merged-away brokers, marked
    `mergeable=False`) and `contacts` every `broker_identity_contacts` row: both
    maps the rule consults — which names a contact belongs to, and how many firms a
    name appears at — are corpus-wide statements, so a filtered input silently
    changes the verdict rather than just shrinking the output.
    """
    by_id = {i.id: i for i in identities}
    keys = {i.id: name_key(i.name) for i in identities}
    blocked = suppressed_pairs if suppressed_pairs is not None else frozenset()
    decision = MergeDecision()

    # 1. The discrimination test. A value is discriminating when every carrier it
    #    has anywhere in the corpus resolves to the SAME single non-null name; an
    #    unnamed carrier abstains rather than poisoning it (it is evidence of
    #    nothing), a differently-named one — merged away or not — kills it.
    carriers: dict[tuple[str, str], set[int]] = {}
    names_at: dict[tuple[str, str], set[str]] = {}
    for c in contacts:
        if c.identity_id not in by_id:
            continue
        value = (c.kind, c.value)
        carriers.setdefault(value, set()).add(c.identity_id)
        seen = names_at.setdefault(value, set())
        if keys[c.identity_id]:
            seen.add(keys[c.identity_id])

    # 2. A-edges: chain (not clique) the mergeable carriers of a discriminating
    #    value that actually carry its one name. n-1 edges instead of n(n-1)/2 —
    #    union-find produces the same component from either.
    a_edges: dict[tuple[int, int], set[str]] = {}
    discriminating: dict[int, dict[str, set[str]]] = {}
    for value, holders in carriers.items():
        names = names_at.get(value) or set()
        if len(names) != 1:
            continue
        for holder in holders:
            discriminating.setdefault(holder, {}).setdefault(value[0], set()).add(value[1])
        only = next(iter(names))
        members = sorted(i for i in holders if keys[i] == only and by_id[i].mergeable)
        for other in members[1:]:
            a_edges.setdefault((members[0], other), set()).add(f"{value[0]}:{value[1]}")

    # 3. B-edges: a name that exists at exactly one firm corpus-wide, chained
    #    across the mergeable identities sitting at that firm under that name. The
    #    firm spread counts EVERY identity — a duplicate of the same person at a
    #    second firm is exactly the ambiguity this path must refuse. Two firms the
    #    spread deliberately refuses to argue from: a FRANCHISE firm is one brand
    #    over many independent offices, so a shared firm_id there is not a shared
    #    employer at all, and an identity with no firm of its own abstains rather
    #    than voting (it has no firm evidence either way).
    firms_of_name: dict[str, set[int]] = {}
    for ident in identities:
        key = keys[ident.id]
        if key and ident.firm_id is not None:
            firms_of_name.setdefault(key, set()).add(ident.firm_id)
    at_firm: dict[tuple[str, int], list[int]] = {}
    for ident in identities:
        key = keys[ident.id]
        if not key or ident.firm_id is None or not ident.mergeable or ident.franchise:
            continue
        if len(firms_of_name.get(key, ())) != 1:
            continue
        at_firm.setdefault((key, ident.firm_id), []).append(ident.id)
    b_edges: set[tuple[int, int]] = set()
    for members_at in at_firm.values():
        members = sorted(set(members_at))
        if len(members) < 2 or _contradicted(members, discriminating):
            continue
        for other in members[1:]:
            b_edges.add((members[0], other))

    # 4. The operator's standing NO removes the edge from the MERGE — but the pair
    #    stays in the component (step 5), because removing it outright would strand
    #    whichever identity the suppression happened to detach from the chain's hub:
    #    suppressing (1, 2) of a 1-2/1-3 chain merged 1 with 3 and left 2 neither
    #    merged nor reviewed, while suppressing (2, 3) of the same corpus downgraded
    #    all three. The only difference was which id sorted first.
    all_edges = sorted(set(a_edges) | b_edges)
    edges: list[tuple[int, int]] = []
    for edge in all_edges:
        if edge in blocked:
            decision.suppressed.append(edge)
            continue
        edges.append(edge)

    # 5. Components over EVERY edge (see step 4); only the surviving ones merge.
    blocked_nodes = {n for pair in blocked for n in pair}
    components = _union_find({n for e in all_edges for n in e}, all_edges)
    root_of = {n: root for root, members in components.items() for n in members}
    edges_by_root: dict[int, list[tuple[int, int]]] = {}
    for edge in edges:
        edges_by_root.setdefault(root_of[edge[0]], []).append(edge)

    merged_root: dict[int, int] = {}
    for root, members in sorted(components.items()):
        if len(members) < 2:
            continue
        group = sorted(members)
        inside = set(group)
        # A suppression ANYWHERE inside the component, not just on an edge: the
        # pure layer can only drop the edge, and union-find would still land the
        # two on one broker through a third identity.
        touched = bool(inside & blocked_nodes) and any(
            lo in inside and hi in inside for lo, hi in blocked)
        if touched or len(group) > MAX_AUTO_MERGE_COMPONENT:
            decision.review_pairs.extend(
                _pairs(group) if len(group) <= MAX_AUTO_MERGE_COMPONENT
                else edges_by_root.get(root, []))
            continue
        decision.auto_merge_groups.append(group)
        for member in group:
            merged_root[member] = root
        inner = edges_by_root.get(root, [])
        present = {
            REASON_CONTACT_NAME: any(e in a_edges for e in inner),
            REASON_NAME_FIRM: any(e in b_edges for e in inner),
        }
        reason = "+".join(r for r in REASON_ORDER if present[r])
        decision.group_reasons[tuple(group)] = reason
        # 6. The dominant case is a two-identity group over one edge carrying one
        #    contact: name it so broker_merge_events records WHAT merged them.
        #    Anything ambiguous (several edges, or one edge with two values) stays
        #    unstamped rather than guessing which contact was decisive; a group
        #    formed by firm rarity alone has no contact to name.
        if len(inner) == 1 and inner[0] in a_edges and len(a_edges[inner[0]]) == 1:
            kind, _, value = next(iter(a_edges[inner[0]])).partition(":")
            decision.group_bridges[tuple(group)] = (kind, value)

    # 7. The residue the operator gets asked about: same name, a contact in common,
    #    but that contact belongs to more than one name. Only across FIRMS — a
    #    same-name same-firm group is already the name_firm candidate tab, and two
    #    cards for one question is worse than none. A cross-name pair produces
    #    nothing at all: the rule never proposes it, so there is no question.
    for value, holders in carriers.items():
        if len(names_at.get(value) or ()) < 2:
            continue
        same_name: dict[str, list[int]] = {}
        for i in sorted(holders):
            key = keys[i]
            if key and by_id[i].mergeable:
                same_name.setdefault(key, []).append(i)
        for cohort in same_name.values():
            for a, b in _pairs(cohort):
                # The name_firm generator groups on brokers.primary_firm_id and
                # INNER JOINs firms, so it cards a pair only when BOTH sides carry
                # that column. Comparing anything else here (the identity's own
                # firm, say) drops the pair from this queue without it appearing in
                # the other one — invisible in both.
                firm_a, firm_b = by_id[a].primary_firm_id, by_id[b].primary_firm_id
                if firm_a is not None and firm_a == firm_b:
                    continue
                if a in merged_root and merged_root[a] == merged_root.get(b):
                    continue  # already unified this run — nothing left to ask
                decision.review_pairs.append((a, b))

    # Stable, de-duplicated review + suppression output. The blocked filter runs
    # HERE too, not only on the edge loop: the downgrades above expand a component
    # pairwise and those transitive pairs never passed through it. Left unfiltered,
    # an unmerge-origin suppression became a brand-new review card every sweep (no
    # prior candidate row blocks it — the status='proposed' guard only stops
    # re-proposing a row that already exists) and was counted in queued_for_review
    # AND suppressed at the same time.
    review = {p if p[0] < p[1] else (p[1], p[0]) for p in decision.review_pairs}
    decision.review_pairs = sorted(p for p in review if p not in blocked)
    decision.suppressed = sorted(set(decision.suppressed) | {p for p in review if p in blocked})
    decision.dismiss_pairs = sorted(set(decision.dismiss_pairs))
    return decision
