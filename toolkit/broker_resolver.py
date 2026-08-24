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
            R: the name appears at no more than one firm corpus-wide
            OR
            F: they share a firm )
      — and no personal contacts disagree (the contradiction veto guards
        every path that argues from absence).

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

**Name rarity** (path R) is the presumption flip the operator directed
(2026-08-24): a same-name cohort is ONE PERSON unless the contacts disagree —
provided the name is rare, meaning it appears at no more than one firm in the
whole corpus. No co-location evidence is required at all: not a shared firm row
(earlier revisions demanded one, which structurally orphaned every ceskereality
record — that portal publishes no e-mail, so its identities never join a firm),
not a shared contact value (a later revision demanded that instead, which still
missed pairs whose ledgers held only different desk lines, and records with no
contacts at all). Rarity is the entire warrant: a name confined to one firm and
absent from the rest of a nine-portal market is overwhelmingly one person, and
the contradiction veto below catches the remainder. Common names (`Jan Novák`,
present at dozens of firms) fail rarity and merge only on path A evidence.

**Shared firm** (path F) drops the rarity requirement INSIDE a firm: a same-name
cohort at one firm is one person unless their personal contacts disagree, no
matter how many OTHER firms carry the name. Rarity guards the cross-firm
question — is the record at ANOTHER firm the same human? — but a within-firm
cohort never asks it, and holding six "Václav Kučera" records at one agency
hostage to a namesake at a different agency answered nothing a reviewer could
judge either (2026-08-24: the entire post-rarity name_firm residue was this
shape). Common labels at DIFFERENT firms still never fuse — F only operates
inside one firm — and the veto still refuses a cohort with disagreeing personal
contacts, which is what keeps five agents published under their firm's name as
five people. The accepted residual: two same-named colleagues at one agency
reachable only through office contacts pool into one broker — a same-name,
same-firm attribution error, mild and reversible.

**Contradiction reads only PERSONAL contacts.** An e-mail whose local part is a
department word (`info@`, `prodej@`, `pronajmy@`, `garaze@`, …
`ROLE_EMAIL_LOCALPARTS`) identifies a desk, not a person — one broker running
five department mailboxes on his own domain is otherwise indistinguishable from
five colleagues (the veto misread exactly that, five single-name mailboxes at one
firm, as five people). A phone published by an identity whose EVERY e-mail is
such a department address is presumed the desk's line and is excluded too; an
identity with no e-mail at all keeps its phones in the veto (a phone-only portal
must still be able to refuse two same-named people with their own mobiles). The
asymmetry is deliberate: a department mailbox can still PROVE sameness (a
single-name mailbox is a valid A bridge) — it just can never prove difference.

The claim it rests on is narrower than "the name appears nowhere else", and three
guards keep it honest. The firm is the identity's OWN (its e-mail domain), so a
merge cannot collapse the very spread that proved a name spans two firms; an
identity that publishes no firm abstains from the spread rather than voting, so
independent brokers with no firm at all neither help nor block it. And a cohort
whose members carry disconfirming contacts — each a
discriminating one of the same kind, no value in common — is refused whole: that
is positive evidence of different people, and it is what a firm-branded display
name ("PREXIMA nemovitosti s.r.o." on five agents) otherwise walks straight past,
since a label that IS the firm's name is unique to that firm by construction.
The same contradiction veto is what guards a FRANCHISE firm (`firms.is_franchise`:
`re-max.cz` is one brand over ~95 independent offices, so a shared firm_id there is
not a shared employer): two same-named agents at two offices surface as different
personal contacts and refuse, while a cohort with NO distinguishing evidence at
all — role inboxes only, the name absent from the rest of the market — merges,
because nothing we hold or could ever hold tells its records apart and the error,
if real, is same-name same-brand attribution: mild and reversible. The rule does
not consult `is_franchise` (an earlier revision refused franchise firms outright,
which silently parked 92% of the name_firm review queue — 1,481 of 1,615 cards on
2026-08-24 — behind a flag meant for firm display).

The paths are OR'd, and the rarity test guards R ONLY — it is R's warrant, not an
extra bar on A. A shared discriminating contact merges a common-named pair even if
that name exists at fifty firms.

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
REASON_NAME_RARITY = "name_rarity"
# Canonical order for a combined reason, so 'contact_name+name_firm' is the ONE
# spelling in the ledger (the orchestrator joins the same way for a chained merge).
# ('contact_rarity' existed for one unreleased revision and never reached the
# ledger — no sweep ran between its merge and this replacement.)
REASON_ORDER = (REASON_CONTACT_NAME, REASON_NAME_FIRM, REASON_NAME_RARITY)

# E-mail local parts that name a desk, not a person. Consulted ONLY by the
# contradiction veto (never by the A/C bridges — a single-name department mailbox
# still proves sameness); extend when a new department word shows up in review.
ROLE_EMAIL_LOCALPARTS = frozenset({
    "info", "kontakt", "kancelar", "office", "recepce", "sekretariat",
    "reality", "rk", "makler", "makleri", "obchod", "podpora", "servis",
    "prodej", "prodeje", "pronajem", "pronajmy", "najem", "najmy",
    "byty", "domy", "pozemky", "garaze", "komerce", "komercni",
    "zakaznicka", "linka", "zakaznickalinka",
})


def _role_email(value: str) -> bool:
    return value.partition("@")[0] in ROLE_EMAIL_LOCALPARTS


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
    the next sweep. `primary_firm_id` is the broker-level rollup and is used for
    ONE thing: not double-carding a pair the `name_firm` candidate generator
    (which groups on exactly that column) already proposes.
    """

    id: int
    source: str
    name: str | None = None
    firm_id: int | None = None
    mergeable: bool = True
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
    # review pair -> WHY the engine held it, for the operator's card. code is one
    # of 'multi_firm' (detail: sorted firm ids the name spans), 'contradicted'
    # (detail: the disagreeing personal values), 'oversized', 'suppressed'
    # (detail: empty). Every review_pairs entry gets one.
    pair_holds: dict[tuple[int, int], tuple[str, list]] = field(default_factory=dict)


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
    emails_of: dict[int, list[str]] = {}
    for c in contacts:
        if c.identity_id not in by_id:
            continue
        value = (c.kind, c.value)
        carriers.setdefault(value, set()).add(c.identity_id)
        seen = names_at.setdefault(value, set())
        if keys[c.identity_id]:
            seen.add(keys[c.identity_id])
        if c.kind == "email":
            emails_of.setdefault(c.identity_id, []).append(c.value)

    # The name -> firms spread, built before any edge: paths B and C both consult
    # it, and it is a corpus-wide statement (every identity votes, merged away or
    # not; an identity with no firm abstains).
    firms_of_name: dict[str, set[int]] = {}
    for ident in identities:
        key = keys[ident.id]
        if key and ident.firm_id is not None:
            firms_of_name.setdefault(key, set()).add(ident.firm_id)

    # 2. A-edges: chain (not clique) the mergeable carriers of a discriminating
    #    value that actually carry its one name. n-1 edges instead of n(n-1)/2 —
    #    union-find produces the same component from either.
    a_edges: dict[tuple[int, int], set[str]] = {}
    discriminating: dict[int, dict[str, set[str]]] = {}
    for value, holders in carriers.items():
        names = names_at.get(value) or set()
        if len(names) != 1:
            continue
        # The contradiction map holds PERSONAL discriminating contacts only: a
        # department mailbox (role local part) names a desk, and a phone whose
        # identity publishes nothing but department mailboxes is presumed the
        # desk's line — one broker's five department addresses must not read as
        # five people. The A bridge below still uses the value regardless: a
        # single-name mailbox proves sameness, it just cannot prove difference.
        for holder in holders:
            if value[0] == "email" and _role_email(value[1]):
                continue
            mails = emails_of.get(holder)
            if value[0] == "phone" and mails and all(_role_email(m) for m in mails):
                continue
            discriminating.setdefault(holder, {}).setdefault(value[0], set()).add(value[1])
        only = next(iter(names))
        members = sorted(i for i in holders if keys[i] == only and by_id[i].mergeable)
        for other in members[1:]:
            a_edges.setdefault((members[0], other), set()).add(f"{value[0]}:{value[1]}")

    # 2b. R-edges: name rarity alone — the presumption flip. Every mergeable
    #     identity of a rare name chains, firm or no firm, shared value or none:
    #     the warrant is the name's confinement to (at most) one firm in a
    #     nine-portal corpus, and the contradiction veto is the brake. No
    #     co-location evidence is consulted — demanding a shared firm row
    #     orphaned every ceskereality record (no e-mail -> no firm), demanding a
    #     shared value missed different-desk-line pairs and contactless records.
    r_edges: set[tuple[int, int]] = set()
    by_key: dict[str, list[int]] = {}
    for ident in identities:
        key = keys[ident.id]
        if key and ident.mergeable:
            by_key.setdefault(key, []).append(ident.id)
    for key, members_at in by_key.items():
        if len(firms_of_name.get(key, ())) > 1:
            continue
        members = sorted(set(members_at))
        if len(members) < 2 or _contradicted(members, discriminating):
            continue
        for other in members[1:]:
            r_edges.add((members[0], other))

    # 3. F-edges: a same-name cohort at ONE firm chains outright — rarity is
    #    the cross-firm gate and a within-firm cohort never asks the cross-firm
    #    question. The contradiction veto below is the only brake (disagreeing
    #    personal contacts = different colleagues who share a name); a cohort
    #    reachable only through office contacts merges, which is the accepted
    #    same-name same-firm residual. Common labels at DIFFERENT firms still
    #    never fuse: F never crosses a firm boundary.
    at_firm: dict[tuple[str, int], list[int]] = {}
    for ident in identities:
        key = keys[ident.id]
        if not key or ident.firm_id is None or not ident.mergeable:
            continue
        at_firm.setdefault((key, ident.firm_id), []).append(ident.id)
    f_edges: set[tuple[int, int]] = set()
    for (key, _firm), members_at in at_firm.items():
        members = sorted(set(members_at))
        if len(members) < 2 or _contradicted(members, discriminating):
            continue
        for other in members[1:]:
            f_edges.add((members[0], other))

    # 4. The operator's standing NO removes the edge from the MERGE — but the pair
    #    stays in the component (step 5), because removing it outright would strand
    #    whichever identity the suppression happened to detach from the chain's hub:
    #    suppressing (1, 2) of a 1-2/1-3 chain merged 1 with 3 and left 2 neither
    #    merged nor reviewed, while suppressing (2, 3) of the same corpus downgraded
    #    all three. The only difference was which id sorted first.
    all_edges = sorted(set(a_edges) | f_edges | r_edges)
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
            downgraded = (_pairs(group) if len(group) <= MAX_AUTO_MERGE_COMPONENT
                          else edges_by_root.get(root, []))
            code = "suppressed" if touched else "oversized"
            for pair in downgraded:
                decision.pair_holds.setdefault(pair, (code, []))
            decision.review_pairs.extend(downgraded)
            continue
        decision.auto_merge_groups.append(group)
        for member in group:
            merged_root[member] = root
        inner = edges_by_root.get(root, [])
        present = {
            REASON_CONTACT_NAME: any(e in a_edges for e in inner),
            REASON_NAME_FIRM: any(e in f_edges for e in inner),
            REASON_NAME_RARITY: any(e in r_edges for e in inner),
        }
        reason = "+".join(r for r in REASON_ORDER if present[r])
        decision.group_reasons[tuple(group)] = reason
        # 6. The dominant case is a two-identity group over one edge carrying one
        #    contact: name it so broker_merge_events records WHAT merged them.
        #    Anything ambiguous (several edges, or one edge with two values) stays
        #    unstamped rather than guessing which contact was decisive; a group
        #    formed by firm rarity alone has no contact to name.
        if len(inner) == 1:
            stamped = a_edges.get(inner[0], set())
            if len(stamped) == 1:
                kind, _, value = next(iter(stamped)).partition(":")
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
                pair = (a, b)
                # Why is this pair here and not merged? With F and R in place
                # there are exactly two possibilities for a same-name pair: the
                # name spans several firms (rarity refused, and the two sit at
                # different firms so F never saw them together), or it does not —
                # in which case R would have chained them unless the cohort is
                # CONTRADICTED. Stamp the reason so the card can show it.
                key = keys[a]
                spread = firms_of_name.get(key or "", set())
                if len(spread) > 1:
                    decision.pair_holds.setdefault(
                        pair, ("multi_firm", sorted(spread)))
                else:
                    vals = sorted(
                        v for m in (a, b)
                        for kvs in discriminating.get(m, {}).values() for v in kvs)
                    decision.pair_holds.setdefault(pair, ("contradicted", vals))
                decision.review_pairs.append(pair)

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
