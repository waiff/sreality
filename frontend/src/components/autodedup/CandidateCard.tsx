/* AUTODEDUP · one CANDIDATE GROUP — the Groups card, on the adverts the engine did NOT merge.
 *
 * WHY IT EXISTS. The residual queue asks one question per PAIR, and the pairs of
 * one generation are not independent questions: one advert against each member
 * of a merged group is the same question five times. The server packs those
 * pairs into cards (E56); this renders one, with the card the operator already
 * knows from /autodedup/groups.
 *
 * THREE THINGS ARE DIFFERENT FROM THAT CARD, and the card says each of them out
 * loud rather than leaving the operator to notice:
 *
 * 1. THE LETTERS START APART. On the groups queue every member starts on A,
 *    because the engine proposed they are one property and the operator is
 *    checking that. Here the engine proposed NOTHING — these adverts were left
 *    separate — so each unit starts on its own letter and the operator brings
 *    together what belongs together. A card that opened with everything on A
 *    would be putting the engine's answer in the operator's mouth, backwards.
 *
 * 2. AN ALREADY-MERGED GROUP IS ONE LOCKED UNIT. Its adverts are bracketed
 *    ("už sloučeno · skupina #900") and share ONE letter select, because
 *    separating two of them is a statement about that g4 group and the Groups
 *    page is where a group is split — with a cluster verdict, which this card
 *    does not write. The server refuses an assignment that splits a lock, so the
 *    control refuses it first, which is the friendlier half of the same rule.
 *
 * 3. TWO SHORTCUTS, AND THE SAVE IS STILL A PRESS. "Vše je jedna jednotka" and
 *    "Nic k sobě nepatří" are the two answers that cover most cards; they set
 *    the letters and nothing else. Nothing is stored until "Uložit rozhodnutí",
 *    because every crossing pair becomes a permanent must-not-link and a
 *    one-click save over eight adverts is 28 permanent facts.
 *
 * BLIND BY DEFAULT (E55). This card carries no judge artefact at all — the queue
 * payload has none — and the drill-down carries the mode on the URL.
 */

import type {
  AutodedupCandidate,
  AutodedupCandidateMember,
  AutodedupCandidateUnit,
  AutodedupVerdictRow,
} from '@/lib/api';
import { fmtCount } from '@/lib/format';
import { Chip, fmtScore } from '@/components/autodedup/EvidenceChips';
import MemberGrid from '@/components/autodedup/MemberGrid';
import {
  DEFAULT_RELATION,
  SplitRow,
  UNIT_LETTERS,
  UnitSelect,
  unitOf,
  type SplitControls,
  type SplitState,
  type UnitMap,
} from '@/components/autodedup/UnitSplit';

/* THE CARD'S OPENING ASSIGNMENT: one letter per unit, in the order the server
 * packed them. Not all-A (the engine did not merge these) and not one letter per
 * ADVERT (the adverts of a merged group are one unit and must share a letter). */
export function candidateDefaultSplit(units: ReadonlyArray<AutodedupCandidateUnit>): SplitState {
  const map: UnitMap = {};
  units.forEach((unit, index) => {
    const letter = UNIT_LETTERS[index] ?? UNIT_LETTERS[UNIT_LETTERS.length - 1];
    for (const listingId of unit.listing_ids) map[listingId] = letter;
  });
  return { units: map, relation: DEFAULT_RELATION, relations: {} };
}

/* A STORED RULING CAN DISAGREE WITH A LOCK. The assignment is read back off the
 * members' pair verdicts (E50), and the operator may have split that merged
 * group on the Groups page — so the letters that come back can separate two
 * adverts this card holds locked together. The card cannot honour both, and it
 * is not the surface that owns the group: it collapses the lock onto its first
 * member's letter, so the controls say what the card will actually send and the
 * save is not a 400 the operator cannot act on. The Groups-page ruling is
 * untouched either way — this route never writes inside a lock. */
export function respectLocks(
  state: SplitState,
  units: ReadonlyArray<AutodedupCandidateUnit>,
): SplitState {
  const map: UnitMap = { ...state.units };
  let changed = false;
  for (const unit of units) {
    if (unit.listing_ids.length < 2) continue;
    const letter = unitOf(map, unit.listing_ids[0]);
    for (const listingId of unit.listing_ids) {
      if (map[listingId] !== letter) changed = true;
      map[listingId] = letter;
    }
  }
  return changed ? { ...state, units: map } : state;
}

/* Every letter in play on this card — what the shortcuts write and what the
 * relation matrix is built from. */
export function allOneUnit(units: ReadonlyArray<AutodedupCandidateUnit>): UnitMap {
  const map: UnitMap = {};
  for (const unit of units) for (const id of unit.listing_ids) map[id] = 'A';
  return map;
}

export function everyUnitApart(units: ReadonlyArray<AutodedupCandidateUnit>): UnitMap {
  return candidateDefaultSplit(units).units;
}

function zoneChip(zones: AutodedupCandidate['zones']): string {
  const parts = Object.entries(zones)
    .filter(([, n]) => (n ?? 0) > 0)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([zone, n]) => `${zone} ${fmtCount(n ?? 0)}`);
  return parts.join(' · ');
}

export default function CandidateCard({
  candidate,
  split,
  eager,
  verdict,
  onOpen,
  onShortcut,
}: {
  candidate: AutodedupCandidate;
  split: SplitControls;
  eager: boolean;
  /* The optimistic badge from the last save on THIS card — the split has no
   * cluster row behind it, so this is the page's own receipt, not a stored one. */
  verdict: AutodedupVerdictRow | null;
  onOpen: () => void;
  onShortcut: (units: UnitMap) => void;
}) {
  const membersByUnit = new Map<string, AutodedupCandidateMember[]>();
  for (const member of candidate.members) {
    const key = member.unit_key ?? `l${member.listing_id}`;
    membersByUnit.set(key, [...(membersByUnit.get(key) ?? []), member]);
  }
  const reviewed = candidate.reviewed || verdict != null;
  return (
    <li className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-4 py-3 space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm text-[var(--color-ink)]">
          <span className="font-mono text-[0.78rem]">#{candidate.candidate_key}</span>{' '}
          <span className="text-[var(--color-ink-3)]">
            {fmtCount(candidate.size)} inzerátů · {fmtCount(candidate.n_units)} jednotek ·{' '}
            {candidate.sources.join(' + ')}
          </span>
        </h2>
        {/* THE ENGINE'S OWN EVIDENCE, which blind mode never hides: the score
          * range of the edges that put these adverts on one card, the zones they
          * were scored into, and the evidence families behind them. */}
        <Chip title="Skóre nejslabší a nejsilnější dvojice, která tyto inzeráty spojila">
          {fmtScore(candidate.score_min)} – {fmtScore(candidate.score_max)}
        </Chip>
        {zoneChip(candidate.zones) && (
          <Chip title="Do kterých pásem engine tyto dvojice zařadil">
            {zoneChip(candidate.zones)}
          </Chip>
        )}
        {candidate.family_names.map((family) => (
          <Chip key={family}>{family}</Chip>
        ))}
        <Chip title="Kolik dvojic tato karta rozhoduje, a kolik z nich už má verdikt">
          {fmtCount(candidate.n_pairs_reviewed)} / {fmtCount(candidate.n_pairs)} dvojic
        </Chip>
        {reviewed && (
          <Chip title="Každá dvojice na této kartě už má verdikt">zkontrolováno</Chip>
        )}
        <button
          type="button"
          onClick={onOpen}
          className="ml-auto rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-2 py-1 text-[0.7rem] text-[var(--color-ink-2)] hover:text-[var(--color-ink)]"
        >
          Open
        </button>
      </div>

      <p className="text-[0.7rem] text-[var(--color-ink-3)] max-w-[52rem]">
        Engine tyto inzeráty <strong>nesloučil</strong> — proto každý začíná na vlastním písmenu.
        Dejte stejné písmeno tomu, co je jedna a tatáž jednotka.
      </p>

      <div className="space-y-3">
        {candidate.units.map((unit, index) => {
          const members = membersByUnit.get(unit.unit_key) ?? [];
          if (members.length === 0) return null;
          const locked = unit.cluster_key != null;
          return (
            <div
              key={unit.unit_key}
              className={
                locked
                  ? 'rounded-[var(--radius-sm)] border border-[var(--color-rule-strong)] bg-[var(--color-paper)] px-3 py-2 space-y-2'
                  : 'space-y-2'
              }
            >
              <div className="flex flex-wrap items-center gap-2">
                {locked && (
                  <span className="text-[0.62rem] tracking-[0.08em] uppercase text-[var(--color-ink-3)]">
                    už sloučeno · skupina{' '}
                    <span className="font-mono">#{unit.cluster_key}</span> ·{' '}
                    {fmtCount(members.length)} inzeráty
                  </span>
                )}
                <UnitSelect
                  listingId={unit.listing_ids[0]}
                  units={split.state.units}
                  count={candidate.units.length}
                  label={locked ? 'Jednotka celé skupiny' : `Jednotka #${unit.listing_ids[0]}`}
                  onChange={(letter) => {
                    /* ONE letter for the whole locked unit: the select moves
                      * every advert of that group together, because the server
                      * refuses an assignment that separates them. */
                    for (const id of unit.listing_ids) split.setUnit(id, letter);
                  }}
                />
              </div>
              <MemberGrid
                members={members}
                eager={eager && index === 0}
                /* A card is capped at eight adverts, so the fold never fires —
                  * but the grid keeps it, and a locked group that somehow holds
                  * more would fold rather than run off the page. */
                unfolded
                columns={locked ? 'sm:grid-cols-2 lg:grid-cols-3' : 'sm:grid-cols-2 lg:grid-cols-4'}
              />
            </div>
          );
        })}
      </div>

      {/* The two answers that cover most cards. They set the letters and nothing
        * else — the operator still presses save, because every crossing pair
        * becomes a permanent must-not-link. */}
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => onShortcut(allOneUnit(candidate.units))}
          className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-2.5 py-1 text-[0.7rem] text-[var(--color-ink-2)] hover:text-[var(--color-ink)]"
        >
          Vše je jedna jednotka
        </button>
        <button
          type="button"
          onClick={() => onShortcut(everyUnitApart(candidate.units))}
          className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-2.5 py-1 text-[0.7rem] text-[var(--color-ink-2)] hover:text-[var(--color-ink)]"
        >
          Nic k sobě nepatří
        </button>
        <span className="text-[0.62rem] text-[var(--color-ink-4)]">
          zkratky jen nastaví písmena — uloží se až tlačítkem níž
        </span>
      </div>

      <SplitRow
        members={candidate.members}
        split={split}
        generation={candidate.generation}
        alwaysOpen
        saveLabel="Uložit rozhodnutí"
        mergeBackLabel="Uložit rozhodnutí"
        /* No cluster row here, so no reason chips (§9) — the note still travels. */
        showReasons={false}
      />
    </li>
  );
}
