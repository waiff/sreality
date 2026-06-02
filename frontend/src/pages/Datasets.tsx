/* Datasets: analysis tab over the ceny-nemovitosti price-stats. Pick a dataset
 * (a filter set), see dataset-wide growth + yield, a per-municipality obec
 * choropleth (rent / sale growth, gross yield), and the per-city table. Reads
 * the *_public views only; dataset CRUD lives behind the API. */
import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  fetchCityMetrics,
  fetchChoropleth,
  fetchDatasets,
  priceStatsKeys,
  type PriceStatCityMetric,
  type PriceStatDataset,
} from '@/lib/priceStats';
import DatasetMap, { METRICS, type DatasetMetric } from '@/components/DatasetMap';

const METRIC_ORDER: DatasetMetric[] = ['rent_cagr_pct', 'sale_cagr_pct', 'gross_yield_pct'];

const fmtPct = (n: number | null | undefined): string =>
  n == null || !Number.isFinite(n) ? '—' : `${n.toFixed(1)}%`;

/* Values are already Kč/m² (rent: Kč/m²/month) — just format the integer. */
const fmtPerM2 = (n: number | null | undefined): string =>
  n == null || !Number.isFinite(n) ? '—' : n.toLocaleString('cs-CZ');

const median = (xs: number[]): number | null => {
  const v = xs.filter((x) => Number.isFinite(x)).sort((a, b) => a - b);
  if (!v.length) return null;
  const m = Math.floor(v.length / 2);
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
};

export default function Datasets() {
  const [datasetId, setDatasetId] = useState<number | null>(null);
  const [metric, setMetric] = useState<DatasetMetric>('rent_cagr_pct');

  const datasetsQ = useQuery<PriceStatDataset[], Error>({
    queryKey: priceStatsKeys.datasets,
    queryFn: fetchDatasets,
    staleTime: 60_000,
  });

  const datasets = datasetsQ.data ?? [];
  const activeId = datasetId ?? datasets[0]?.id ?? null;
  const active = datasets.find((d) => d.id === activeId) ?? null;

  const metricsQ = useQuery<PriceStatCityMetric[], Error>({
    queryKey: priceStatsKeys.cityMetrics(activeId ?? -1),
    queryFn: () => fetchCityMetrics(activeId as number),
    enabled: activeId != null,
    staleTime: 60_000,
  });

  const choroplethQ = useQuery({
    queryKey: priceStatsKeys.choropleth(activeId ?? -1),
    queryFn: () => fetchChoropleth(activeId as number),
    enabled: activeId != null,
    staleTime: 60_000,
  });

  const cities = metricsQ.data ?? [];
  const summary = useMemo(() => {
    const rent = median(cities.map((c) => c.rent_cagr_pct ?? NaN));
    const sale = median(cities.map((c) => c.sale_cagr_pct ?? NaN));
    const yld = median(cities.map((c) => c.gross_yield_pct ?? NaN));
    const latest = cities
      .map((c) => c.rent_latest_ym ?? c.sale_latest_ym)
      .filter(Boolean)
      .sort()
      .at(-1);
    return { rent, sale, yld, latest, count: cities.length };
  }, [cities]);

  const sortedCities = useMemo(
    () =>
      [...cities].sort((a, b) => (b[metric] ?? -Infinity) - (a[metric] ?? -Infinity)),
    [cities, metric],
  );

  return (
    <div className="px-6 py-8 max-w-6xl mx-auto">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="text-xs tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
            ceny nemovitostí
          </p>
          <h1 className="mt-1 text-2xl">Datasets</h1>
          <p className="mt-1 text-sm text-[var(--color-ink-3)]">
            Rental & sale-price growth and gross yield per municipality, per filter set.
          </p>
        </div>
        <DatasetPicker
          datasets={datasets}
          value={activeId}
          onChange={(id) => setDatasetId(id)}
          loading={datasetsQ.isLoading}
        />
      </header>

      {active && <FilterChips dataset={active} />}

      {datasetsQ.isLoading ? (
        <p className="mt-10 text-sm text-[var(--color-ink-3)]">Loading datasets…</p>
      ) : datasets.length === 0 ? (
        <EmptyDatasets />
      ) : (
        <>
          <SummaryCards summary={summary} loading={metricsQ.isLoading} />

          <div className="mt-8 flex items-center gap-2">
            {METRIC_ORDER.map((m) => (
              <button
                key={m}
                onClick={() => setMetric(m)}
                className={`px-3 py-1.5 text-sm rounded-sm border transition-colors ${
                  metric === m
                    ? 'border-[var(--color-accent)] text-[var(--color-accent)]'
                    : 'border-[var(--color-line)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-1)]'
                }`}
              >
                {METRICS[m].label}
              </button>
            ))}
          </div>

          <div className="mt-3">
            {cities.length === 0 && !metricsQ.isLoading ? (
              <EmptyData />
            ) : (
              <DatasetMap polygons={choroplethQ.data ?? []} metric={metric} />
            )}
          </div>

          {cities.length > 0 && (
            <CityTable cities={sortedCities} highlight={metric} />
          )}
        </>
      )}
    </div>
  );
}

function DatasetPicker({
  datasets, value, onChange, loading,
}: {
  datasets: PriceStatDataset[];
  value: number | null;
  onChange: (id: number) => void;
  loading: boolean;
}) {
  if (loading || datasets.length === 0) return null;
  return (
    <select
      value={value ?? ''}
      onChange={(e) => onChange(Number(e.target.value))}
      className="text-sm border border-[var(--color-line)] rounded-sm bg-[var(--color-paper)] px-3 py-2 max-w-xs"
    >
      {datasets.map((d) => (
        <option key={d.id} value={d.id}>{d.name}</option>
      ))}
    </select>
  );
}

const COND: Record<string, string> = {
  '1': 'velmi dobrý', '2': 'dobrý', '6': 'novostavba', '8': 'před rekonstrukcí',
  '9': 'po rekonstrukci',
};
const CONSTR: Record<string, string> = { '5': 'panel', '2': 'cihla', '10': 'ostatní' };
const OWN: Record<string, string> = { '1': 'osobní', '2': 'družstevní', '3': 'státní' };

function FilterChips({ dataset }: { dataset: PriceStatDataset }) {
  const chips: string[] = [];
  if (dataset.building_condition) chips.push(COND[dataset.building_condition] ?? `stav ${dataset.building_condition}`);
  if (dataset.building_type) chips.push(CONSTR[dataset.building_type] ?? `konstr. ${dataset.building_type}`);
  if (dataset.ownership) chips.push(OWN[dataset.ownership] ?? `vl. ${dataset.ownership}`);
  if (dataset.usable_area_from != null || dataset.usable_area_to != null)
    chips.push(`${dataset.usable_area_from ?? 0}–${dataset.usable_area_to ?? '∞'} m²`);
  if (dataset.distance) chips.push(`okolí ${dataset.distance} km`);
  if (!chips.length) return null;
  return (
    <div className="mt-3 flex flex-wrap gap-1.5">
      {chips.map((c) => (
        <span key={c} className="text-xs px-2 py-0.5 rounded-sm border border-[var(--color-line)] text-[var(--color-ink-3)]">
          {c}
        </span>
      ))}
    </div>
  );
}

function SummaryCards({
  summary, loading,
}: {
  summary: { rent: number | null; sale: number | null; yld: number | null; latest?: string | null; count: number };
  loading: boolean;
}) {
  const cards = [
    { label: 'Rent growth p.a. (median)', value: fmtPct(summary.rent) },
    { label: 'Sale-price growth p.a. (median)', value: fmtPct(summary.sale) },
    { label: 'Gross yield (median)', value: fmtPct(summary.yld) },
  ];
  return (
    <div className="mt-6 grid grid-cols-1 sm:grid-cols-3 gap-3">
      {cards.map((c) => (
        <div key={c.label} className="border border-[var(--color-line)] rounded-sm p-4">
          <div className="text-xs text-[var(--color-ink-3)]">{c.label}</div>
          <div className="mt-1 text-2xl tabular-nums">{loading ? '…' : c.value}</div>
        </div>
      ))}
      <p className="sm:col-span-3 text-xs text-[var(--color-ink-3)]">
        Across {summary.count} municipalities{summary.latest ? ` · latest data ${summary.latest}` : ''}.
        Median across cities; thin markets (few active offers) are excluded from the map.
      </p>
    </div>
  );
}

function CityTable({
  cities, highlight,
}: {
  cities: PriceStatCityMetric[];
  highlight: DatasetMetric;
}) {
  const hl = (m: DatasetMetric) =>
    m === highlight ? 'text-[var(--color-accent)]' : '';
  return (
    <div className="mt-8 overflow-x-auto">
      <table className="w-full text-sm border-collapse">
        <thead>
          <tr className="text-left text-[var(--color-ink-3)] border-b border-[var(--color-line)]">
            <th className="py-2 pr-4 font-normal">Municipality</th>
            <th className="py-2 pr-4 font-normal text-right">Sale Kč/m²</th>
            <th className={`py-2 pr-4 font-normal text-right ${hl('sale_cagr_pct')}`}>Sale growth p.a.</th>
            <th className="py-2 pr-4 font-normal text-right">Rent Kč/m²/mo</th>
            <th className={`py-2 pr-4 font-normal text-right ${hl('rent_cagr_pct')}`}>Rent growth p.a.</th>
            <th className={`py-2 pr-4 font-normal text-right ${hl('gross_yield_pct')}`}>Gross yield</th>
          </tr>
        </thead>
        <tbody className="tabular-nums">
          {cities.map((c) => {
            const thin = (c.rent_min_active ?? 0) < 3 || (c.sale_min_active ?? 0) < 3;
            return (
              <tr
                key={`${c.entity_type}-${c.entity_id}`}
                className={`border-b border-[var(--color-line)]/60 ${thin ? 'text-[var(--color-ink-3)]' : ''}`}
              >
                <td className="py-1.5 pr-4">{c.locality_name}{thin && <span title="thin market"> ·</span>}</td>
                <td className="py-1.5 pr-4 text-right">{fmtPerM2(c.sale_latest_price)}</td>
                <td className={`py-1.5 pr-4 text-right ${hl('sale_cagr_pct')}`}>{fmtPct(c.sale_cagr_pct)}</td>
                <td className="py-1.5 pr-4 text-right">{fmtPerM2(c.rent_latest_price)}</td>
                <td className={`py-1.5 pr-4 text-right ${hl('rent_cagr_pct')}`}>{fmtPct(c.rent_cagr_pct)}</td>
                <td className={`py-1.5 pr-4 text-right ${hl('gross_yield_pct')}`}>{fmtPct(c.gross_yield_pct)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function EmptyDatasets() {
  return (
    <div className="mt-10 border border-dashed border-[var(--color-line)] rounded-sm p-8 text-center">
      <p className="text-sm text-[var(--color-ink-2)]">No datasets yet.</p>
      <p className="mt-1 text-xs text-[var(--color-ink-3)]">
        Create one via the API (<code>POST /price-stats/datasets</code>); it populates on the next price-stats run.
      </p>
    </div>
  );
}

function EmptyData() {
  return (
    <div className="border border-dashed border-[var(--color-line)] rounded-sm p-8 text-center">
      <p className="text-sm text-[var(--color-ink-2)]">This dataset has no data yet.</p>
      <p className="mt-1 text-xs text-[var(--color-ink-3)]">
        It populates on the next <code>scrape_price_stats</code> run.
      </p>
    </div>
  );
}
