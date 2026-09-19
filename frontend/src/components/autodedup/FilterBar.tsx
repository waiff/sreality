/* AUTODEDUP · the filter bar every validation queue wears, and the count under it.
 *
 * EVERY CONTROL OFFERS A VOCABULARY, NEVER A CODE. "Town/quarter" is a RÚIAN
 * bigint and shipping it as a text field meant typing a town name produced NaN,
 * which the query layer drops: the filter silently did nothing. Each select here
 * offers what exists, so a typo cannot quietly empty the queue.
 *
 * A CONTROL THIS SURFACE DOES NOT SEND MUST NOT BE RENDERED. An inert select
 * that returns the same list is worse than no select at all — which is why the
 * portal, category and verdict groups are switchable: the residual queue filters
 * by a source PAIR and takes no category keys, and the candidate queue's verdict
 * vocabulary is reviewed/unreviewed rather than the five verdicts.
 */

import { type ReactNode } from 'react';

import { fmtCount } from '@/lib/format';
import BlockSelect from '@/components/autodedup/BlockSelect';
import GenerationSelect from '@/components/autodedup/GenerationSelect';
import { type GroupFilterState } from '@/components/autodedup/filterState';

export const FILTER_LABEL = 'text-[0.6rem] tracking-[0.12em] uppercase text-[var(--color-ink-3)]';
export const FILTER_CONTROL =
  'mt-0.5 w-full rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-2 py-1 text-[0.75rem] text-[var(--color-ink)]';

/* The portal vocabulary is the backend's `listings.source` enum; the page only
 * offers the nine that exist rather than a free-text box, so a typo can never
 * silently return an empty queue. */
export const SOURCES = [
  'sreality',
  'bazos',
  'bezrealitky',
  'idnes',
  'mmreality',
  'remax',
  'ceskereality',
  'realitymix',
  'maxima',
];

export function FilterBar<T extends GroupFilterState>({
  value,
  onChange,
  children,
  /* A control this surface does not SEND must not be rendered. */
  showSource = true,
  showCategory = true,
  /* The five-verdict select is the GROUPS and PAIRS vocabulary. The candidate
   * queue has no verdict of its own — a card is reviewed when every pair inside
   * it is — so it hides this one and offers its own two-value control instead. */
  showVerdict = true,
  /* GROUPS ONLY. "změněno od verdiktu" asks for the groups whose membership has
   * moved since the operator ruled them (E58) — a question only a cluster-grain
   * queue can answer, and one the residual queue's server refuses. */
  showChangedVerdict = false,
}: {
  /* Generic over the filter state so a surface with extra keys of its own (the
   * residual view's zone + portal pair) keeps them through every edit made
   * here — a non-generic bar would spread them away on the first keystroke. */
  value: T;
  onChange: (next: T) => void;
  children?: ReactNode;
  showSource?: boolean;
  showCategory?: boolean;
  showVerdict?: boolean;
  showChangedVerdict?: boolean;
}) {
  const set = <K extends keyof T>(key: K, v: T[K]) => onChange({ ...value, [key]: v });
  return (
    <div className="mt-4 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-4 py-3">
      <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <GenerationSelect
          value={value.generation}
          onChange={(next) => set('generation', next as T['generation'])}
          labelClassName={FILTER_LABEL}
          controlClassName={FILTER_CONTROL}
        />
        <BlockSelect
          value={value.block}
          onChange={(next) => set('block', next as T['block'])}
          /* '' resolves server-side to the newest pass — the same answer the
           * queue itself gets, so the vocabulary can never be another pass's. */
          generation={value.generation || null}
          labelClassName={FILTER_LABEL}
          controlClassName={FILTER_CONTROL}
        />
        {showSource && (
          <label className="block">
            <span className={FILTER_LABEL}>Portal</span>
            <select
              className={FILTER_CONTROL}
              value={value.source}
              onChange={(e) => set('source', e.target.value as T['source'])}
            >
              <option value="">vše</option>
              {SOURCES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
        )}
        {showCategory && (
          <label className="block">
            <span className={FILTER_LABEL}>Druh</span>
            <select
              className={FILTER_CONTROL}
              value={value.category_main}
              onChange={(e) => set('category_main', e.target.value as T['category_main'])}
            >
              <option value="">vše</option>
              <option value="byt">byt</option>
              <option value="dum">dům</option>
              <option value="pozemek">pozemek</option>
              <option value="komercni">komerční</option>
              <option value="ostatni">ostatní</option>
            </select>
          </label>
        )}
        {showCategory && (
          <label className="block">
            <span className={FILTER_LABEL}>Nabídka</span>
            <select
              className={FILTER_CONTROL}
              value={value.category_type}
              onChange={(e) => set('category_type', e.target.value as T['category_type'])}
            >
              <option value="">vše</option>
              <option value="prodej">prodej</option>
              <option value="pronajem">pronájem</option>
              <option value="drazba">dražba</option>
            </select>
          </label>
        )}
        {showVerdict && (
          <label className="block">
            <span className={FILTER_LABEL}>Verdict</span>
            <select
              className={FILTER_CONTROL}
              value={value.verdict}
              onChange={(e) => set('verdict', e.target.value as T['verdict'])}
            >
              <option value="">vše</option>
              <option value="unreviewed">unreviewed</option>
              {showChangedVerdict && (
                <option value="changed">změněno od verdiktu</option>
              )}
              <option value="same">same</option>
              <option value="different">different</option>
              <option value="same_building_different_unit">same building</option>
              <option value="same_project_different_unit">same project</option>
              <option value="unsure">unsure</option>
            </select>
          </label>
        )}
        {children}
      </div>
    </div>
  );
}

/* HOW MUCH OF THE QUEUE IS ON SCREEN. A keyset page cannot count itself, so the
 * total arrives with the first page and the loaded rows are counted here. When
 * the server sent no count the loaded number is still said plainly — "20 groups
 * loaded" — because a silent list gives no sense of the work left, and a
 * fabricated total would be worse than none. The noun agrees with the number it
 * follows: "1 groups loaded" is the kind of seam that makes a careful page read
 * as a generated one. */
const plural = (n: number, noun: string): string => (n === 1 ? noun.replace(/s$/, '') : noun);

export function ResultCount({
  shown,
  total,
  noun,
}: {
  shown: number;
  total: number | null;
  noun: string;
}) {
  return (
    <p className="mt-4 text-[0.72rem] text-[var(--color-ink-3)] tabular-nums">
      {total == null
        ? `${fmtCount(shown)} ${plural(shown, noun)} loaded`
        : `${fmtCount(shown)} of ${fmtCount(total)} ${plural(total, noun)}`}
    </p>
  );
}

export default FilterBar;
