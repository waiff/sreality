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
 * vocabulary is reviewed/unreviewed rather than the operator's three.
 */

import { type ReactNode } from 'react';

import type { RulingTown } from '@/lib/api';
import { fmtCount } from '@/lib/format';
import BlockSelect from '@/components/autodedup/BlockSelect';
import GenerationSelect from '@/components/autodedup/GenerationSelect';
import { type GroupFilterState } from '@/components/autodedup/filterState';

export const FILTER_LABEL = 'text-[0.6rem] tracking-[0.12em] uppercase text-[var(--color-ink-3)]';
export const FILTER_CONTROL =
  'mt-0.5 w-full rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-2 py-1 text-[0.75rem] text-[var(--color-ink)]';

/* "label (n)" — a count beside an option is the current filter's rows with
 * that value (the server's facets); absent, the label alone. */
export const counted = (label: string, n: number | null | undefined): string =>
  n == null ? label : `${label} (${fmtCount(n)})`;

export interface FilterOption {
  value: string;
  label: string;
  count?: number | null;
}

/* THE ONE FILTER SELECT of the review pages: a caption, "vše" (no filter) and
 * the vocabulary, each option with its count when the page has one. `children`
 * carries what a flat list cannot (the towns' two optgroups). */
export function FilterSelect({
  label,
  value,
  onChange,
  options = [],
  allLabel = 'vše',
  children,
}: {
  label: string;
  value: string;
  onChange: (next: string) => void;
  options?: ReadonlyArray<FilterOption>;
  allLabel?: string | null;
  children?: ReactNode;
}) {
  return (
    <label className="block">
      <span className={FILTER_LABEL}>{label}</span>
      <select className={FILTER_CONTROL} value={value} onChange={(e) => onChange(e.target.value)}>
        {allLabel != null && <option value="">{allLabel}</option>}
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {counted(o.label, o.count)}
          </option>
        ))}
        {children}
      </select>
    </label>
  );
}

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

/* The town filter (`o:<obec>` / `c:<část obce>`): the towns the listed rows
 * touch, busiest first, in two groups — and the value a link carried even when
 * the capped list does not name it, so a shared link keeps its filter. */
export function TownSelect({
  value,
  towns,
  onChange,
}: {
  value: string;
  towns: ReadonlyArray<RulingTown>;
  onChange: (next: string) => void;
}) {
  const group = (grain: 'o' | 'c', label: string) => {
    const shown = towns.filter((t) => t.grain === grain);
    return shown.length === 0 ? null : (
      <optgroup label={label}>
        {shown.map((t) => (
          <option key={`${t.grain}:${t.code}`} value={`${t.grain}:${t.code}`}>
            {counted(t.name ?? String(t.code), t.n)}
          </option>
        ))}
      </optgroup>
    );
  };
  return (
    <FilterSelect label="Obec / část" value={value} onChange={onChange} allLabel="všude">
      {group('o', 'Obce')}
      {group('c', 'Části obce')}
      {value && !towns.some((t) => `${t.grain}:${t.code}` === value) && (
        <option value={value}>{value}</option>
      )}
    </FilterSelect>
  );
}

const CATEGORIES: ReadonlyArray<FilterOption> = [
  { value: 'byt', label: 'byt' },
  { value: 'dum', label: 'dům' },
  { value: 'pozemek', label: 'pozemek' },
  { value: 'komercni', label: 'komerční' },
  { value: 'ostatni', label: 'ostatní' },
];

const DEALS: ReadonlyArray<FilterOption> = [
  { value: 'prodej', label: 'prodej' },
  { value: 'pronajem', label: 'pronájem' },
  { value: 'drazba', label: 'dražba' },
];

export function FilterBar<T extends GroupFilterState>({
  value,
  onChange,
  children,
  /* A control this surface does not SEND must not be rendered. */
  showSource = true,
  showCategory = true,
  /* The three-verdict select is the GROUPS and PAIRS vocabulary (D39). The
   * candidate queue has no verdict of its own — a card is reviewed when every
   * pair inside it is — so it hides this one and offers its own two-value
   * control instead. */
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
          <FilterSelect
            label="Portál"
            value={value.source}
            onChange={(next) => set('source', next as T['source'])}
            options={SOURCES.map((s) => ({ value: s, label: s }))}
          />
        )}
        {showCategory && (
          <FilterSelect
            label="Druh"
            value={value.category_main}
            onChange={(next) => set('category_main', next as T['category_main'])}
            options={CATEGORIES}
          />
        )}
        {showCategory && (
          <FilterSelect
            label="Nabídka"
            value={value.category_type}
            onChange={(next) => set('category_type', next as T['category_type'])}
            options={DEALS}
          />
        )}
        {showVerdict && (
          /* ONE WORD OVER THREE STORED VALUES: the server widens `different`
           * over the two finer values migration 532 wrote, so a ruling taken
           * before D39 stays in its own queue. */
          <FilterSelect
            label="Rozhodnutí"
            value={value.verdict}
            onChange={(next) => set('verdict', next as T['verdict'])}
            options={[
              { value: 'unreviewed', label: 'nezkontrolováno' },
              ...(showChangedVerdict
                ? [{ value: 'changed', label: 'změněno od verdiktu' }]
                : []),
              { value: 'same', label: 'stejné' },
              { value: 'different', label: 'různé' },
              { value: 'unsure', label: 'nevím' },
            ]}
          />
        )}
        {children}
      </div>
    </div>
  );
}

/* HOW MUCH OF THE QUEUE IS ON SCREEN. A keyset page cannot count itself, so the
 * total arrives with the first page and the loaded rows are counted here. When
 * the server sent no count the loaded number is still said plainly — "načteno
 * 20 skupin" — because a silent list gives no sense of the work left, and a
 * fabricated total would be worse than none. The noun agrees with its number
 * (1 / 2–4 / 5+), and "z N" takes the genitive. */
const NOUNS: Record<'groups' | 'pairs', readonly [string, string, string]> = {
  groups: ['skupina', 'skupiny', 'skupin'],
  pairs: ['dvojice', 'dvojice', 'dvojic'],
};

export function ResultCount({
  shown,
  total,
  noun,
}: {
  shown: number;
  total: number | null;
  noun: 'groups' | 'pairs';
}) {
  const [one, few, many] = NOUNS[noun];
  return (
    <p className="mt-4 text-[0.72rem] text-[var(--color-ink-3)] tabular-nums">
      {total == null
        ? `načteno ${fmtCount(shown)} ${shown === 1 ? one : shown >= 2 && shown <= 4 ? few : many}`
        : `${fmtCount(shown)} z ${fmtCount(total)} ${total === 1 ? few : many}`}
    </p>
  );
}

export default FilterBar;
