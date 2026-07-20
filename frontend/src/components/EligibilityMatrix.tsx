import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import FilterChip from '@/components/FilterChip';
import { getEligibilityMatrix, type DedupPathKey } from '@/lib/api';
import {
  PATHS,
  PATH_BY_KEY,
  cellFilter,
  drillIsExact,
  pivotPath,
  pivotSummary,
  type Cell,
  type CellFilter,
  type CellPart,
  type MatrixScope,
  type MatrixView,
  type PathInput,
} from '@/lib/dedupPaths';
import { CATEGORY_MAIN_LABELS, CATEGORY_MAIN_ORDER } from '@/lib/dedupFunnel';
import { filterById } from '@/lib/filterRegistry.generated';
import { portalShort } from '@/lib/portals';
import { fmtCount } from '@/lib/format';

/* Where dedup loses the inventory — property type × portal, one pass at a time.
 *
 * The per-listing cards below answer "why is THIS row unreachable?" one at a time.
 * This answers the question the operator actually opens the page with: which portal
 * and which property type leak listings out of the engine, and through WHICH missing
 * field. Every number is a link — clicking it applies the filters that reproduce
 * exactly the listings it counted, so a suspicious count is one click from its rows.
 *
 * One 0.8s grouped scan backs all of it: the API returns the joint distribution of
 * the eligibility inputs and every tab here is a client-side pivot of it (lib/dedupPaths).
 */

/* Stable per-reason colour from the categorical tag family — the same input key keeps
 * its colour across every tab, so the loss rule becomes readable rather than decorative. */
const REASON_TINT: Record<string, string> = {
  obec: 'var(--color-tag-brick)',
  geom: 'var(--color-tag-slate)',
  area: 'var(--color-tag-ochre)',
  disposition: 'var(--color-tag-plum)',
  street: 'var(--color-tag-teal)',
  active: 'var(--color-tag-sand)',
};
const MULTI_TINT = 'var(--color-rule-strong)';

type TabKey = 'summary' | DedupPathKey;

const TABS: ReadonlyArray<{ id: TabKey; label: string }> = [
  { id: 'summary', label: 'Souhrn — žádná cesta' },
  ...PATHS.map((p) => ({ id: p.key as TabKey, label: p.label })),
];

const SCOPES: ReadonlyArray<{ id: MatrixScope; label: string }> = [
  { id: 'active', label: 'Aktivní' },
  { id: 'all', label: 'Vše (i neaktivní)' },
];

const SUMMARY_EXPLAIN =
  'Bottom line: listingy, které nedokáže zařadit ŽÁDNÁ ze tří cest — nikdy se s ničím neporovnají. ' +
  'Důvod se přičítá cestě, která byla NEJBLÍŽ (nejméně chybějících vstupů, při shodě vyhrává cesta ' +
  'určená pro danou rodinu). Proto se u pozemku neukazuje „chybí dispozice" — pozemek ji mít nikdy ' +
  'nebude, jeho skutečná mezera je ta v geo cestě.';

export default function EligibilityMatrix({ onPick }: { onPick: (f: CellFilter) => void }) {
  const [tab, setTab] = useState<TabKey>('summary');
  const [scope, setScope] = useState<MatrixScope>('active');

  const q = useQuery({
    queryKey: ['location-audit', 'eligibility-matrix'],
    queryFn: getEligibilityMatrix,
    // A full grouped scan; the shape of the inventory does not move minute to minute.
    staleTime: 5 * 60_000,
  });

  const spec = tab === 'summary' ? null : PATH_BY_KEY[tab];

  const view: MatrixView | null = useMemo(() => {
    if (!q.data) return null;
    // The server derives each pass's domain + active gate from the same constants it
    // renders into SQL — prefer them over this module's fallback copies.
    const meta = q.data.paths.find((p) => p.key === tab);
    const resolved =
      spec && meta
        ? { ...spec, domain: meta.domain_categories, activeOnly: meta.active_only }
        : spec;
    return resolved
      ? pivotPath(q.data.buckets, resolved, scope)
      : pivotSummary(q.data.buckets, scope);
  }, [q.data, tab, scope, spec]);

  const sources = useMemo(() => orderSources(view?.sources ?? []), [view?.sources]);
  const categories = useMemo(() => orderCategories(view?.categories ?? []), [view?.categories]);

  const pick = (category: string, source: string, part: CellPart) =>
    onPick(cellFilter(spec, scope, category, source, part));

  return (
    <section className="mt-6 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)]">
      <div className="px-5 pt-4 pb-3 border-b border-[var(--color-rule)]">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h2 className="text-base">Kde dedup ztrácí inventář</h2>
          <span className="text-[0.72rem] text-[var(--color-ink-4)]">
            typ nemovitosti × portál · každé číslo je odkaz do výpisu níže
          </span>
        </div>

        <div className="mt-3 flex flex-wrap gap-1.5">
          {TABS.map((t) => (
            <FilterChip key={t.id} on={tab === t.id} label={t.label} onClick={() => setTab(t.id)} />
          ))}
        </div>
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          <span className="text-[0.62rem] uppercase tracking-[0.1em] text-[var(--color-ink-4)] mr-1">
            Rozsah
          </span>
          {SCOPES.map((s) => (
            <FilterChip
              key={s.id}
              on={scope === s.id}
              label={s.label}
              onClick={() => setScope(s.id)}
            />
          ))}
        </div>

        <p className="mt-2.5 text-[0.76rem] leading-relaxed text-[var(--color-ink-2)] max-w-3xl">
          {spec ? spec.explain : SUMMARY_EXPLAIN}
        </p>

        {view && view.inputs.length > 0 && <Legend inputs={view.inputs} />}
      </div>

      {q.isLoading ? (
        <p className="px-5 py-6 text-sm text-[var(--color-ink-3)]">Počítám matici…</p>
      ) : q.isError ? (
        <p className="px-5 py-6 text-sm text-[var(--color-ink-3)]">
          Matici se nepodařilo načíst.
        </p>
      ) : !view || categories.length === 0 ? (
        <p className="px-5 py-6 text-sm text-[var(--color-ink-3)]">Žádná data pro tento rozsah.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-left">
            <thead>
              <tr className="bg-[var(--color-paper-3)]">
                <th className="sticky left-0 z-20 bg-[var(--color-paper-3)] px-3 py-2 text-[0.62rem] uppercase tracking-[0.1em] font-normal text-[var(--color-ink-4)] border-b border-[var(--color-rule)]">
                  Typ
                </th>
                {sources.map((s) => (
                  <th
                    key={s}
                    className="px-2 py-2 text-[0.68rem] font-medium text-[var(--color-ink-2)] border-b border-l border-[var(--color-rule)] min-w-[7.5rem]"
                  >
                    {portalShort(s)}
                  </th>
                ))}
                <th className="px-2 py-2 text-[0.68rem] font-medium text-[var(--color-ink)] border-b border-l border-[var(--color-rule-strong)] min-w-[7.5rem]">
                  Celkem
                </th>
              </tr>
            </thead>
            <tbody>
              {categories.map((cat) => (
                <MatrixRow
                  key={cat}
                  category={cat}
                  label={CATEGORY_MAIN_LABELS[cat] ?? cat}
                  sources={sources}
                  view={view}
                  onPick={pick}
                  exactFor={(part) => drillIsExact(spec, part)}
                />
              ))}
              <MatrixRow
                category=""
                label="Celkem"
                sources={sources}
                view={view}
                onPick={pick}
                exactFor={(part) => drillIsExact(spec, part)}
                isTotal
              />
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Legend({ inputs }: { inputs: PathInput[] }) {
  return (
    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1">
      {inputs.map((i) => (
        <span
          key={i.key}
          className="inline-flex items-center gap-1.5 text-[0.66rem] text-[var(--color-ink-3)] cursor-help"
          title={i.tip}
        >
          <span
            aria-hidden
            className="inline-block w-2.5 h-2.5 rounded-[2px]"
            style={{ backgroundColor: REASON_TINT[i.key] ?? MULTI_TINT }}
          />
          {i.label}
        </span>
      ))}
      <span
        className="inline-flex items-center gap-1.5 text-[0.66rem] text-[var(--color-ink-4)] cursor-help"
        title="Chybí dva a více vstupů najednou — doplnit jedno pole by nestačilo."
      >
        <span
          aria-hidden
          className="inline-block w-2.5 h-2.5 rounded-[2px]"
          style={{ backgroundColor: MULTI_TINT }}
        />
        více vstupů
      </span>
    </div>
  );
}

function MatrixRow({
  category,
  label,
  sources,
  view,
  onPick,
  exactFor,
  isTotal,
}: {
  category: string;
  label: string;
  sources: string[];
  view: MatrixView;
  onPick: (category: string, source: string, part: CellPart) => void;
  exactFor: (part: CellPart) => boolean;
  isTotal?: boolean;
}) {
  const rowBg = isTotal ? 'bg-[var(--color-paper-3)]' : 'bg-[var(--color-paper-2)]';
  const topRule = isTotal ? 'border-t-[var(--color-rule-strong)]' : 'border-t-[var(--color-rule)]';
  return (
    <tr className={rowBg}>
      <th
        scope="row"
        className={`sticky left-0 z-10 ${rowBg} px-3 py-2 align-top border-t ${topRule} text-[0.76rem] font-medium ${
          isTotal ? 'text-[var(--color-ink)]' : 'text-[var(--color-ink-2)]'
        }`}
      >
        {label}
      </th>
      {[...sources, ''].map((src) => (
        <td
          key={src || '__total__'}
          className={`px-2 py-2 align-top border-t border-l ${topRule} ${
            src === '' ? 'border-l-[var(--color-rule-strong)]' : 'border-l-[var(--color-rule)]'
          }`}
        >
          <CellBody
            cell={view.cells[category]?.[src]}
            inputs={view.inputs}
            onPick={(part) => onPick(category, src, part)}
            exactFor={exactFor}
          />
        </td>
      ))}
    </tr>
  );
}

/* One cell: how many listings the pass should reach, how many it loses, and through
 * which hole — plus the loss rule, whose full width is the scope and whose filled,
 * reason-segmented part is the loss. The bar is read before the digits are. */
function CellBody({
  cell,
  inputs,
  onPick,
  exactFor,
}: {
  cell: Cell | undefined;
  inputs: PathInput[];
  onPick: (part: CellPart) => void;
  exactFor: (part: CellPart) => boolean;
}) {
  if (!cell || cell.scope === 0) {
    return <span className="text-[0.72rem] text-[var(--color-ink-4)]">—</span>;
  }

  const share = cell.ineligible / cell.scope;
  const title = (part: CellPart, body: string) =>
    body +
    (exactFor(part)
      ? '\n\nKliknutím se výpis níže omezí přesně na tyto listingy.'
      : '\n\nKliknutím se výpis omezí na nedosažitelné listingy, kterým toto pole chybí — nadmnožina tohoto čísla. Číslo samo přičítá důvod NEJBLIŽŠÍ cestě, což se filtrem vyjádřit nedá.');
  const ranked = inputs
    .map((i) => ({ input: i, n: cell.reasons[i.key] ?? 0 }))
    .filter((r) => r.n > 0)
    .sort((a, b) => b.n - a.n);
  const shown = ranked.slice(0, 3);
  const restN = ranked.slice(3).reduce((s, r) => s + r.n, 0) + cell.multi;

  return (
    <div className="flex flex-col gap-0.5">
      <Num
        n={cell.scope}
        title={title('scope', 'Listingy, které by touto cestou měly projít (rozsah cesty).')}
        cls="text-[var(--color-ink-4)] text-[0.7rem]"
        onClick={() => onPick('scope')}
      />
      <Num
        n={cell.ineligible}
        title={title(
          'ineligible',
          `Nezpůsobilé — cesta je nedokáže načíst (${pct(share)} rozsahu).`,
        )}
        cls={`text-[0.86rem] ${
          cell.ineligible === 0 ? 'text-[var(--color-sage)]' : 'text-[var(--color-brick)]'
        }`}
        onClick={() => onPick('ineligible')}
        suffix={
          cell.ineligible > 0 ? (
            <span className="ml-1 text-[0.62rem] text-[var(--color-ink-4)]">{pct(share)}</span>
          ) : null
        }
      />

      <LossRule cell={cell} ranked={ranked} />

      {shown.map(({ input, n }) => (
        <Num
          key={input.key}
          n={n}
          title={title({ reason: input.key }, input.tip)}
          cls="text-[0.62rem] text-[var(--color-ink-3)]"
          onClick={() => onPick({ reason: input.key })}
          suffix={
            <span className="ml-1 text-[var(--color-ink-4)]">
              <span
                aria-hidden
                className="inline-block w-1.5 h-1.5 rounded-[1px] mr-1 align-middle"
                style={{ backgroundColor: REASON_TINT[input.key] ?? MULTI_TINT }}
              />
              {input.label}
            </span>
          }
        />
      ))}
      {restN > 0 && (
        <Num
          n={restN}
          title={title(
            'multi',
            'Chybí dva a více vstupů najednou (nebo méně časté důvody) — jedno doplněné pole by nestačilo.',
          )}
          cls="text-[0.62rem] text-[var(--color-ink-4)]"
          onClick={() => onPick('multi')}
          suffix={<span className="ml-1">více vstupů</span>}
        />
      )}
      {cell.unexplained > 0 && (
        <span
          className="text-[0.62rem] text-[var(--color-ochre)] cursor-help"
          title="Nezpůsobilé, ale žádný sledovaný vstup nechybí — znamená to, že se seznam vstupů v UI rozešel se serverovým predikátem. Nahlásit, ne ignorovat."
        >
          {fmtCount(cell.unexplained)} nevysvětleno
        </span>
      )}
    </div>
  );
}

function LossRule({ cell, ranked }: { cell: Cell; ranked: Array<{ input: PathInput; n: number }> }) {
  if (cell.scope === 0) return null;
  const segs = [
    ...ranked.map((r) => ({ key: r.input.key, n: r.n, tint: REASON_TINT[r.input.key] ?? MULTI_TINT })),
    { key: '__multi__', n: cell.multi, tint: MULTI_TINT },
  ].filter((s) => s.n > 0);

  return (
    <div
      className="h-[3px] w-full rounded-[1px] bg-[var(--color-rule-soft)] flex overflow-hidden my-0.5"
      aria-hidden
    >
      {segs.map((s) => (
        <span
          key={s.key}
          className="shrink-0"
          style={{ width: `${(s.n / cell.scope) * 100}%`, backgroundColor: s.tint }}
        />
      ))}
    </div>
  );
}

function Num({
  n,
  title,
  cls,
  onClick,
  suffix,
}: {
  n: number;
  title: string;
  cls: string;
  onClick: () => void;
  suffix?: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className={`text-left font-mono tabular-nums hover:underline decoration-dotted underline-offset-2 ${cls}`}
    >
      {fmtCount(n)}
      {suffix}
    </button>
  );
}

const pct = (v: number): string =>
  v === 0 ? '0 %' : v < 0.001 ? '<0,1 %' : `${(v * 100).toFixed(v < 0.1 ? 1 : 0).replace('.', ',')} %`;

/* Columns follow the SAME portal order as the page's Zdroj chips (one generated enum),
 * so the operator's eye moves the same way in both places. */
function orderSources(found: string[]): string[] {
  const order = (filterById('portals')?.enum_values ?? []).map((o) => String(o.value));
  const rank = new Map(order.map((s, i) => [s, i]));
  return [...found].sort(
    (a, b) => (rank.get(a) ?? 999) - (rank.get(b) ?? 999) || a.localeCompare(b),
  );
}

function orderCategories(found: string[]): string[] {
  const rank = new Map((CATEGORY_MAIN_ORDER as readonly string[]).map((c, i) => [c, i]));
  return [...found].sort(
    (a, b) => (rank.get(a) ?? 999) - (rank.get(b) ?? 999) || a.localeCompare(b),
  );
}
