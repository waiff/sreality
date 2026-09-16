/* AUTODEDUP · the one row of evidence chips.
 *
 * Every validation surface answers the same question — "on what grounds?" — so
 * it answers it with the same chips: which certificate fired (E24, a structural
 * rule, not a learned weight), which EVIDENCE FAMILIES carry a present signal
 * (E11: images alone can never merge a pair, which is only legible if the
 * families are visible), the weakest edge in the group, whether a judge has
 * ruled, and the shared-photo warning that marks a developer catalogue.
 *
 * THE FAMILY BITMASK IS A CONTRACT, not a display detail. `families` is stored
 * as a smallint by the score lane, and the bit table below must match
 * `autodedup/score_lane.py::FAMILY_BITS` exactly — a re-numbered bit would
 * relabel history rather than break, so the mapping is stated here in full
 * instead of being inferred from the order of anything.
 */

import { type ReactNode } from 'react';

import type { AutodedupZone } from '@/lib/api';

/* Mirror of score_lane.FAMILY_BITS. PRICE and TIME are not `features.
 * EVIDENCE_FAMILIES` members — the engine's E11 gate counts the five it names —
 * but the score lane stores all seven, so all seven are decodable here. */
export const FAMILY_BITS: ReadonlyArray<readonly [number, string]> = [
  [1, 'ATTR'],
  [2, 'PRICE'],
  [4, 'TXT'],
  [8, 'BRK'],
  [16, 'LOC'],
  [32, 'IMG'],
  [64, 'TIME'],
];

/* Accepts either spelling: the stored bitmask, or the names the API decodes for
 * some payloads. One decoder, so a chip row cannot depend on which route the
 * data came from. */
export function familyNames(value: number | string[] | null | undefined): string[] {
  if (Array.isArray(value)) return value;
  if (value == null) return [];
  return FAMILY_BITS.filter(([bit]) => (value & bit) !== 0).map(([, name]) => name);
}

/* A probability, not a count: two decimals, cs-CZ comma. `fmtCount` would round
 * 0.004 to "0" and a score of exactly zero is a different statement. */
const scoreFmt = new Intl.NumberFormat('cs-CZ', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

export const fmtScore = (n: number | null | undefined): string =>
  n == null ? '—' : scoreFmt.format(n);

type Tone = 'neutral' | 'good' | 'warn' | 'bad';

const TONE_CLASS: Record<Tone, string> = {
  neutral:
    'border-[var(--color-rule)] bg-[var(--color-paper)] text-[var(--color-ink-2)]',
  good: 'border-[var(--color-sage)] bg-[var(--color-sage-soft)] text-[var(--color-sage)]',
  warn: 'border-[var(--color-ochre)] bg-[var(--color-ochre-soft)] text-[var(--color-ochre)]',
  bad: 'border-[var(--color-brick)] bg-[var(--color-brick-soft)] text-[var(--color-brick)]',
};

export function Chip({
  tone = 'neutral',
  title,
  children,
}: {
  tone?: Tone;
  title?: string;
  children: ReactNode;
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 rounded-[var(--radius-xs)] border px-1.5 py-0.5 text-[0.65rem] ${TONE_CLASS[tone]}`}
    >
      {children}
    </span>
  );
}

const ZONE_TONE: Record<AutodedupZone, Tone> = {
  merge: 'good',
  band: 'warn',
  reject: 'neutral',
};

export interface EvidenceChipsProps {
  zone?: AutodedupZone | null;
  score?: number | null;
  /* The weakest edge of a GROUP — labelled apart from a pair's own score,
   * because "the worst link in this cluster" is the number that decides it. */
  minEdgeScore?: number | null;
  certificate?: string | null;
  nCertificates?: number | null;
  families?: number | string[] | null;
  nJudged?: number | null;
  sharedPhoto?: boolean | null;
  maxGapDays?: number | null;
  guardVeto?: string | null;
}

export default function EvidenceChips({
  zone,
  score,
  minEdgeScore,
  certificate,
  nCertificates,
  families,
  nJudged,
  sharedPhoto,
  maxGapDays,
  guardVeto,
}: EvidenceChipsProps) {
  const fams = familyNames(families);
  return (
    <ul className="flex flex-wrap items-center gap-1.5">
      {zone && (
        <li>
          <Chip tone={ZONE_TONE[zone]} title="Which zone the calibrated score fell in">
            {zone}
          </Chip>
        </li>
      )}
      {score != null && (
        <li>
          <Chip title="The model's calibrated probability for this pair">
            <span className="font-mono tabular-nums">p {fmtScore(score)}</span>
          </Chip>
        </li>
      )}
      {minEdgeScore != null && (
        <li>
          <Chip title="The weakest edge inside this group — where the errors live">
            <span className="font-mono tabular-nums">weakest {fmtScore(minEdgeScore)}</span>
          </Chip>
        </li>
      )}
      {certificate && (
        <li>
          <Chip tone="good" title="A structural certificate decided this pair, not the model">
            {certificate}
          </Chip>
        </li>
      )}
      {nCertificates != null && nCertificates > 0 && !certificate && (
        <li>
          <Chip tone="good" title="Edges in this group carried by a structural certificate">
            <span className="tabular-nums">{nCertificates}</span> certificate
            {nCertificates === 1 ? '' : 's'}
          </Chip>
        </li>
      )}
      {fams.map((f) => (
        <li key={f}>
          <Chip title="An evidence family carrying a present, unit-grade signal">{f}</Chip>
        </li>
      ))}
      {nJudged != null && (
        <li>
          <Chip title="Edges an LLM judge has ruled on">
            {nJudged > 0 ? `judged ${nJudged}` : 'not judged'}
          </Chip>
        </li>
      )}
      {maxGapDays != null && maxGapDays > 0 && (
        <li>
          <Chip title="Longest gap between one listing disappearing and the next appearing">
            <span className="tabular-nums">re-listed after {maxGapDays} d</span>
          </Chip>
        </li>
      )}
      {sharedPhoto && (
        <li>
          <Chip tone="warn" title="A photo here appears on many other listings — a developer catalogue">
            shared photos
          </Chip>
        </li>
      )}
      {guardVeto && (
        <li>
          <Chip tone="bad" title="A hard guard vetoed this pair outright">
            veto: {guardVeto}
          </Chip>
        </li>
      )}
    </ul>
  );
}

/* THE CODES, SPELLED OUT ONCE PER PAGE. The chips above are the engine's own
 * vocabulary — family codes, zone words, certificate ids — and each carries a
 * `title`, which no touch device shows and no keyboard reaches. A non-technical
 * operator should not have to hover to read the evidence row, so the legend is
 * on the page in text. */
const LEGEND: ReadonlyArray<readonly [string, string]> = [
  ['ATTR', 'attributes'],
  ['PRICE', 'price'],
  ['TXT', 'description text'],
  ['BRK', 'broker'],
  ['LOC', 'location'],
  ['IMG', 'photos'],
  ['TIME', 'timing'],
];

export function EvidenceLegend() {
  return (
    <p className="mt-3 text-[0.68rem] leading-relaxed text-[var(--color-ink-3)]">
      <span className="text-[var(--color-ink-4)]">Evidence families: </span>
      {LEGEND.map(([code, words], i) => (
        <span key={code}>
          {i > 0 && ' · '}
          <span className="font-mono">{code}</span> = {words}
        </span>
      ))}
      <span className="text-[var(--color-ink-4)]">
        {' '}· zones: <span className="font-mono">merge</span> = above the auto-merge line,{' '}
        <span className="font-mono">band</span> = between the two lines,{' '}
        <span className="font-mono">reject</span> = below them · a{' '}
        <span className="font-mono">K-…</span> chip means a structural certificate decided the
        pair, not the model.
      </span>
    </p>
  );
}
