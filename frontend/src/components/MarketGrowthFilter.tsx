/* Browse filter: keep listings whose municipality (obec) meets/exceeds a
 * rent/sale growth p.a. threshold, computed from a chosen price-stats dataset
 * over a [from, to] window (CAGR ≥, the same math as the map overlay). Each
 * checked dataset is one rule; multiple rules AND. Resolution to an obec_id
 * allowlist happens in queries.ts (resolvePriceGrowthPrefilter). BROWSE-only. */
import { useQuery } from '@tanstack/react-query';
import {
  fetchDatasets,
  priceStatsKeys,
  type PriceStatDataset,
} from '@/lib/priceStats';
import type { PriceGrowthRule } from '@/lib/filters';
import { YmPicker, YM_CUR } from '@/components/YmPicker';

interface Props {
  value: PriceGrowthRule[];
  onChange: (next: PriceGrowthRule[]) => void;
}

const numOrNull = (s: string): number | null => {
  if (s.trim() === '') return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
};

const INPUT_CLS =
  'w-16 text-[0.7rem] tabular-nums bg-[var(--color-paper-2)] border border-[var(--color-rule)] '
  + 'rounded px-1.5 py-0.5 text-right';

export default function MarketGrowthFilter({ value, onChange }: Props) {
  const { data: datasets, isLoading, isError } = useQuery<PriceStatDataset[], Error>({
    queryKey: priceStatsKeys.datasets,
    queryFn: fetchDatasets,
    staleTime: 60_000,
  });

  const ruleFor = (id: number): PriceGrowthRule | undefined =>
    value.find((r) => r.datasetId === id);

  const setRule = (id: number, patch: Partial<PriceGrowthRule>): void => {
    onChange(value.map((r) => (r.datasetId === id ? { ...r, ...patch } : r)));
  };

  const toggle = (d: PriceStatDataset, on: boolean): void => {
    if (on) {
      onChange([
        ...value,
        {
          datasetId: d.id,
          fromYm: d.start_ym ?? '2015-01',
          toYm: d.end_ym ?? YM_CUR,
          rentMinPct: null,
          saleMinPct: null,
        },
      ]);
    } else {
      onChange(value.filter((r) => r.datasetId !== d.id));
    }
  };

  if (isLoading) {
    return <p className="text-[0.7rem] text-[var(--color-ink-3)]">Loading datasets…</p>;
  }
  if (isError) {
    return <p className="text-[0.7rem] text-[var(--color-brick)]">Couldn’t load datasets.</p>;
  }
  if (!datasets || datasets.length === 0) {
    return (
      <p className="text-[0.7rem] text-[var(--color-ink-3)]">
        No datasets yet — create one on the Datasets page.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-1.5">
      <p className="text-[0.65rem] leading-snug text-[var(--color-ink-3)]">
        Keep listings whose municipality’s growth p.a. (CAGR) over the window meets the
        threshold, computed from the chosen dataset. Multiple datasets combine with AND.
      </p>
      <div className="flex flex-col gap-1.5 max-h-72 overflow-y-auto pr-1">
        {datasets.map((d) => {
          const rule = ruleFor(d.id);
          const on = rule != null;
          return (
            <div key={d.id} className="border-b border-[var(--color-rule-soft)] pb-1.5 last:border-0">
              <label className="flex items-center gap-1.5 cursor-pointer text-[0.75rem] text-[var(--color-ink-2)]">
                <input type="checkbox" checked={on} onChange={(e) => toggle(d, e.target.checked)} />
                <span className="truncate" title={d.name}>{d.name}</span>
              </label>
              {on && rule && (
                <div className="mt-1.5 ml-5 flex flex-col gap-1.5">
                  <div className="flex items-center gap-1 text-[0.7rem] text-[var(--color-ink-3)]">
                    <YmPicker value={rule.fromYm ?? d.start_ym ?? '2015-01'} onChange={(v) => setRule(d.id, { fromYm: v })} />
                    <span className="text-[var(--color-ink-3)]">→</span>
                    <YmPicker value={rule.toYm ?? d.end_ym ?? YM_CUR} onChange={(v) => setRule(d.id, { toYm: v })} />
                  </div>
                  <label className="flex items-center justify-between gap-2 text-[0.7rem] text-[var(--color-ink-2)]">
                    <span>Rent growth ≥ %/yr</span>
                    <input
                      type="number" inputMode="decimal" step="0.1"
                      className={INPUT_CLS}
                      value={rule.rentMinPct ?? ''}
                      onChange={(e) => setRule(d.id, { rentMinPct: numOrNull(e.target.value) })}
                    />
                  </label>
                  <label className="flex items-center justify-between gap-2 text-[0.7rem] text-[var(--color-ink-2)]">
                    <span>Sale growth ≥ %/yr</span>
                    <input
                      type="number" inputMode="decimal" step="0.1"
                      className={INPUT_CLS}
                      value={rule.saleMinPct ?? ''}
                      onChange={(e) => setRule(d.id, { saleMinPct: numOrNull(e.target.value) })}
                    />
                  </label>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
