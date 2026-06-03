/* Datasets — aggregate market analysis (sreality ceny-nemovitosti). Pick a
 * dataset (filter set) + a from/to window; see dataset-wide growth + yield,
 * a per-obec choropleth, and a sortable per-city table. Growth is computed
 * live for the chosen window by the price_stat_growth RPC. Reads public
 * views/RPC only; dataset writes go through the API. Civic-archive tokens. */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  fetchDatasets,
  fetchGrowth,
  fetchLatestRun,
  fetchSeries,
  priceStatsKeys,
  type PriceStatDataset,
  type PriceStatGrowthRow,
  type PriceStatRun,
} from '@/lib/priceStats';
import { createPriceStatDataset, deletePriceStatDataset } from '@/lib/api';
import DatasetMap, { METRICS, type DatasetMetric } from '@/components/DatasetMap';
import CityPicker from '@/components/CityPicker';
import { buildHoverData } from '@/lib/growthChoropleth';

const METRIC_ORDER: DatasetMetric[] = ['rent_cagr_pct', 'sale_cagr_pct', 'yield_change_pp_pa'];
const MIN_ACTIVE = 3;
const FIRST_YEAR = 2015;
const now = new Date();
const CUR_YM = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
const DEFAULT_CITY_COUNT = 43; // the standard municipality set when none picked

/* Rough scrape-time estimate, calibrated against real runs:
 * time ≈ 120 s overhead + 1.2 s × cities × ceil(months/24-month chunks).
 * Periodicity doesn't change FETCH time (the API is always monthly; it only
 * changes what we store). Flags runs that won't fit the 60-min workflow cap. */
function estimateScrapeText(cities: number, startYm: string, endYm: string): string {
  const [sy, sm] = startYm.split('-').map(Number);
  const [ey, em] = endYm.split('-').map(Number);
  const months = Math.max(1, ey * 12 + em - (sy * 12 + sm) + 1);
  const chunks = Math.max(1, Math.ceil(months / 24));
  const secs = 120 + 1.2 * Math.max(1, cities) * chunks;
  if (secs > 55 * 60) {
    return `~${(secs / 3600).toFixed(1)} h — exceeds the 60-min run limit, split into smaller runs`;
  }
  return `~${Math.max(1, Math.round(secs / 60))} min`;
}

const fmtPct = (n: number | null | undefined): string =>
  n == null || !Number.isFinite(n) ? '—' : `${n.toFixed(1)}%`;
const fmtPP = (n: number | null | undefined): string =>
  n == null || !Number.isFinite(n) ? '—' : `${n >= 0 ? '+' : ''}${n.toFixed(2)} pp`;
const fmtPerM2 = (n: number | null | undefined): string =>
  n == null || !Number.isFinite(n) ? '—' : n.toLocaleString('cs-CZ');

const median = (xs: number[]): number | null => {
  const v = xs.filter((x) => Number.isFinite(x)).sort((a, b) => a - b);
  if (!v.length) return null;
  const m = Math.floor(v.length / 2);
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
};

const isThin = (r: PriceStatGrowthRow): boolean =>
  (r.rent_min_active ?? 0) < MIN_ACTIVE || (r.sale_min_active ?? 0) < MIN_ACTIVE;

type SortKey =
  | 'locality_name' | 'sale_latest_price' | 'sale_cagr_pct'
  | 'rent_latest_price' | 'rent_cagr_pct' | 'gross_yield_pct' | 'yield_change_pp_pa';

export default function Datasets() {
  const qc = useQueryClient();
  const [datasetId, setDatasetId] = useState<number | null>(null);
  const [metric, setMetric] = useState<DatasetMetric>('rent_cagr_pct');
  const [from, setFrom] = useState(`${FIRST_YEAR}-01`);
  const [to, setTo] = useState(CUR_YM);
  const [showNew, setShowNew] = useState(false);
  const [chartOnHover, setChartOnHover] = useState(false);
  const [sort, setSort] = useState<{ col: SortKey; dir: 'asc' | 'desc' } | null>(null);

  const datasetsQ = useQuery<PriceStatDataset[], Error>({
    queryKey: priceStatsKeys.datasets,
    queryFn: fetchDatasets,
    staleTime: 60_000,
  });
  const datasets = datasetsQ.data ?? [];
  const activeId = datasetId ?? datasets[0]?.id ?? null;
  const active = datasets.find((d) => d.id === activeId) ?? null;

  const growthQ = useQuery<PriceStatGrowthRow[], Error>({
    queryKey: priceStatsKeys.growth(activeId ?? -1, from, to),
    queryFn: () => fetchGrowth(activeId as number, from, to),
    enabled: activeId != null,
    staleTime: 60_000,
  });
  const rows = growthQ.data ?? [];

  // Live scrape status for this dataset — polls while a run is in progress.
  const runQ = useQuery<PriceStatRun | null, Error>({
    queryKey: priceStatsKeys.latestRun(activeId ?? -1),
    queryFn: () => fetchLatestRun(activeId as number),
    enabled: activeId != null,
    refetchInterval: (q) => (q.state.data?.status === 'running' ? 3000 : false),
  });
  // When a run finishes, refetch the derived data so the map/table fill in.
  const prevStatus = useRef<string | undefined>(undefined);
  useEffect(() => {
    const st = runQ.data?.status;
    if (prevStatus.current === 'running' && st === 'success' && activeId != null) {
      qc.invalidateQueries({ queryKey: ['price_stat_growth'] });
      qc.invalidateQueries({ queryKey: ['price_stat_obec_series'] });
    }
    prevStatus.current = st;
  }, [runQ.data?.status, runQ.data?.run_id, activeId, qc]);

  const deleteMutation = useMutation({
    mutationFn: (id: number) => deletePriceStatDataset(id),
    onSuccess: () => {
      setDatasetId(null);
      qc.invalidateQueries({ queryKey: priceStatsKeys.datasets });
    },
  });

  const seriesQ = useQuery({
    queryKey: priceStatsKeys.obecSeries(activeId ?? -1, from, to),
    queryFn: () => fetchSeries(activeId as number, from, to),
    enabled: activeId != null && chartOnHover,
    staleTime: 60_000,
  });
  const hoverData = useMemo(
    () => (seriesQ.data ? buildHoverData(seriesQ.data, metric) : null),
    [seriesQ.data, metric],
  );

  const summary = useMemo(() => ({
    rent: median(rows.map((r) => r.rent_cagr_pct ?? NaN)),
    sale: median(rows.map((r) => r.sale_cagr_pct ?? NaN)),
    yield: median(rows.map((r) => r.gross_yield_pct ?? NaN)),
    count: rows.length,
  }), [rows]);

  const activeSort: { col: SortKey; dir: 'asc' | 'desc' } =
    sort ?? { col: metric === 'sale_cagr_pct' ? 'sale_cagr_pct' : metric === 'yield_change_pp_pa' ? 'yield_change_pp_pa' : 'rent_cagr_pct', dir: 'desc' };

  const sortedRows = useMemo(() => {
    const dir = activeSort.dir === 'asc' ? 1 : -1;
    return [...rows].sort((a, b) => {
      const av = a[activeSort.col];
      const bv = b[activeSort.col];
      if (activeSort.col === 'locality_name') {
        return String(av).localeCompare(String(bv), 'cs') * dir;
      }
      const an = av == null ? null : Number(av);
      const bn = bv == null ? null : Number(bv);
      if (an == null && bn == null) return 0;
      if (an == null) return 1;       // nulls last regardless of dir
      if (bn == null) return -1;
      return (an - bn) * dir;
    });
  }, [rows, activeSort.col, activeSort.dir]);

  const onSort = (col: SortKey) =>
    setSort((s) =>
      s && s.col === col
        ? { col, dir: s.dir === 'desc' ? 'asc' : 'desc' }
        : { col, dir: col === 'locality_name' ? 'asc' : 'desc' });

  return (
    <div className="px-6 py-8 max-w-6xl mx-auto text-[var(--color-ink)]">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="text-xs tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
            ceny nemovitostí
          </p>
          <h1 className="mt-1 text-2xl font-[family-name:var(--font-display)]">Datasets</h1>
          <p className="mt-1 text-sm text-[var(--color-ink-3)]">
            Rent &amp; sale-price growth and gross yield per municipality, for a chosen window.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <DatasetPicker datasets={datasets} value={activeId} onChange={setDatasetId} loading={datasetsQ.isLoading} />
          {active && (
            <button
              onClick={() => {
                if (window.confirm(`Remove dataset “${active.name}”? It disappears from the app (data is kept in the database).`)) {
                  deleteMutation.mutate(active.id);
                }
              }}
              disabled={deleteMutation.isPending}
              title="Remove this dataset"
              className="text-sm border border-[var(--color-rule)] rounded-[var(--radius-sm)] px-3 py-2 text-[var(--color-ink-3)] hover:text-[var(--color-brick)] hover:border-[var(--color-brick)] transition-colors disabled:opacity-50"
            >
              Remove
            </button>
          )}
          <button
            onClick={() => setShowNew((v) => !v)}
            className="text-sm border border-[var(--color-rule)] rounded-[var(--radius-sm)] px-3 py-2 text-[var(--color-ink-2)] hover:text-[var(--color-ink)] hover:border-[var(--color-rule-strong)] transition-colors"
          >
            {showNew ? 'Close' : '+ New dataset'}
          </button>
        </div>
      </header>

      {showNew && (
        <NewDatasetForm
          onClose={() => setShowNew(false)}
          onCreated={(d) => {
            qc.invalidateQueries({ queryKey: priceStatsKeys.datasets });
            setDatasetId(d.id);
            setShowNew(false);
          }}
        />
      )}

      {active && <FilterChips dataset={active} count={summary.count} />}

      {datasetsQ.isLoading ? (
        <p className="mt-10 text-sm text-[var(--color-ink-3)]">Loading datasets…</p>
      ) : datasets.length === 0 ? (
        <EmptyDatasets />
      ) : (
        <>
          <div className="mt-6 flex flex-wrap items-center justify-between gap-4 border border-[var(--color-rule)] rounded-[var(--radius-md)] bg-[var(--color-paper-2)] px-4 py-3">
            <WindowControl from={from} to={to} onFrom={setFrom} onTo={setTo} />
            <div className="flex items-center gap-4">
              <label className="inline-flex items-center gap-1.5 text-sm text-[var(--color-ink-2)] cursor-pointer">
                <input type="checkbox" checked={chartOnHover} onChange={(e) => setChartOnHover(e.target.checked)} />
                Chart on hover
              </label>
              <MetricToggle metric={metric} onChange={setMetric} />
            </div>
          </div>

          {runQ.data && <RunStatusBanner run={runQ.data} />}

          <SummaryBand summary={summary} metric={metric} loading={growthQ.isLoading} />

          <div className="mt-6">
            {rows.length === 0 && !growthQ.isLoading ? (
              <EmptyData run={runQ.data ?? null} />
            ) : (
              <DatasetMap rows={rows} metric={metric} chartOnHover={chartOnHover} hoverData={hoverData} />
            )}
          </div>

          {rows.length > 0 && (
            <CityTable rows={sortedRows} metric={metric} sort={activeSort} onSort={onSort} />
          )}
        </>
      )}
    </div>
  );
}

/* ---- controls ----------------------------------------------------------- */

const SELECT_CLS =
  'text-sm border border-[var(--color-rule)] rounded-[var(--radius-sm)] bg-[var(--color-paper-3)] px-2.5 py-1.5 text-[var(--color-ink)]';

function DatasetPicker({
  datasets, value, onChange, loading,
}: {
  datasets: PriceStatDataset[]; value: number | null; onChange: (id: number) => void; loading: boolean;
}) {
  if (loading || datasets.length === 0) return null;
  return (
    <select value={value ?? ''} onChange={(e) => onChange(Number(e.target.value))}
      className={`${SELECT_CLS} max-w-xs px-3 py-2`}>
      {datasets.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
    </select>
  );
}

const MONTHS = Array.from({ length: 12 }, (_, i) => String(i + 1).padStart(2, '0'));
const YEARS = Array.from({ length: now.getFullYear() - FIRST_YEAR + 1 }, (_, i) => String(FIRST_YEAR + i));

function YmPicker({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const [y, m] = value.split('-');
  return (
    <span className="inline-flex items-center gap-1">
      <select value={y} onChange={(e) => onChange(`${e.target.value}-${m}`)} className={SELECT_CLS}>
        {YEARS.map((yr) => <option key={yr} value={yr}>{yr}</option>)}
      </select>
      <select value={m} onChange={(e) => onChange(`${y}-${e.target.value}`)} className={SELECT_CLS}>
        {MONTHS.map((mo) => <option key={mo} value={mo}>{mo}</option>)}
      </select>
    </span>
  );
}

function WindowControl({
  from, to, onFrom, onTo,
}: {
  from: string; to: string; onFrom: (v: string) => void; onTo: (v: string) => void;
}) {
  return (
    <div className="flex items-center gap-2 text-sm text-[var(--color-ink-2)]">
      <span className="text-xs uppercase tracking-[0.14em] text-[var(--color-ink-3)]">Window</span>
      <YmPicker value={from} onChange={onFrom} />
      <span className="text-[var(--color-ink-3)]">→</span>
      <YmPicker value={to} onChange={onTo} />
    </div>
  );
}

function MetricToggle({ metric, onChange }: { metric: DatasetMetric; onChange: (m: DatasetMetric) => void }) {
  return (
    <div className="inline-flex rounded-[var(--radius-sm)] border border-[var(--color-rule)] overflow-hidden">
      {METRIC_ORDER.map((m, i) => (
        <button
          key={m}
          onClick={() => onChange(m)}
          className={`px-3 py-1.5 text-sm transition-colors ${i > 0 ? 'border-l border-[var(--color-rule)]' : ''} ${
            metric === m
              ? 'bg-[var(--color-copper-soft)] text-[var(--color-copper)]'
              : 'text-[var(--color-ink-3)] hover:text-[var(--color-ink)]'
          }`}
        >
          {METRICS[m].label}
        </button>
      ))}
    </div>
  );
}

/* ---- summary ------------------------------------------------------------ */

function SummaryBand({
  summary, metric, loading,
}: {
  summary: { rent: number | null; sale: number | null; yield: number | null; count: number };
  metric: DatasetMetric;
  loading: boolean;
}) {
  const cards: Array<{ key: DatasetMetric | 'gross'; label: string; value: string }> = [
    { key: 'rent_cagr_pct', label: 'Median rent growth p.a.', value: fmtPct(summary.rent) },
    { key: 'sale_cagr_pct', label: 'Median sale growth p.a.', value: fmtPct(summary.sale) },
    { key: 'gross', label: 'Median gross yield', value: fmtPct(summary.yield) },
  ];
  return (
    <div className="mt-5 grid grid-cols-1 sm:grid-cols-3 gap-3">
      {cards.map((c) => {
        const emph = c.key === metric;
        return (
          <div key={c.label}
            className={`border rounded-[var(--radius-md)] p-4 ${emph ? 'border-[var(--color-copper)] bg-[var(--color-copper-soft)]' : 'border-[var(--color-rule)] bg-[var(--color-paper-2)]'}`}>
            <div className="text-xs text-[var(--color-ink-3)]">{c.label}</div>
            <div className="mt-1 text-3xl tabular-nums font-[family-name:var(--font-display)]">
              {loading ? '…' : c.value}
            </div>
          </div>
        );
      })}
      <p className="sm:col-span-3 text-xs text-[var(--color-ink-3)]">
        Median across {summary.count} municipalities for the chosen window. Thin markets
        (fewer than {MIN_ACTIVE} active offers) are excluded from the map and dimmed in the table.
      </p>
    </div>
  );
}

/* ---- table -------------------------------------------------------------- */

interface Col { key: SortKey; label: string; metric?: DatasetMetric; render: (r: PriceStatGrowthRow) => ReactNode; }

const COLS: Col[] = [
  { key: 'locality_name', label: 'Municipality', render: (r) => r.locality_name },
  { key: 'sale_latest_price', label: 'Sale Kč/m²', render: (r) => fmtPerM2(r.sale_latest_price) },
  { key: 'sale_cagr_pct', label: 'Sale growth', metric: 'sale_cagr_pct', render: (r) => <Delta n={r.sale_cagr_pct} fmt={fmtPct} /> },
  { key: 'rent_latest_price', label: 'Rent Kč/m²/mo', render: (r) => fmtPerM2(r.rent_latest_price) },
  { key: 'rent_cagr_pct', label: 'Rent growth', metric: 'rent_cagr_pct', render: (r) => <Delta n={r.rent_cagr_pct} fmt={fmtPct} /> },
  { key: 'gross_yield_pct', label: 'Gross yield', render: (r) => fmtPct(r.gross_yield_pct) },
  { key: 'yield_change_pp_pa', label: 'Yield change', metric: 'yield_change_pp_pa', render: (r) => <Delta n={r.yield_change_pp_pa} fmt={fmtPP} /> },
];

function Delta({ n, fmt }: { n: number | null; fmt: (n: number | null) => string }) {
  const cls = n == null ? '' : n < 0 ? 'text-[var(--color-brick)]' : '';
  return <span className={cls}>{fmt(n)}</span>;
}

function CityTable({
  rows, metric, sort, onSort,
}: {
  rows: PriceStatGrowthRow[];
  metric: DatasetMetric;
  sort: { col: SortKey; dir: 'asc' | 'desc' };
  onSort: (c: SortKey) => void;
}) {
  return (
    <div className="mt-8 overflow-x-auto border border-[var(--color-rule)] rounded-[var(--radius-md)]">
      <table className="w-full text-sm border-collapse">
        <thead>
          <tr className="border-b border-[var(--color-rule)] bg-[var(--color-paper-2)]">
            {COLS.map((c, i) => {
              const isActiveMetric = c.metric === metric;
              const isSorted = sort.col === c.key;
              return (
                <th key={c.key}
                  onClick={() => onSort(c.key)}
                  className={`px-3 py-2 font-normal cursor-pointer select-none whitespace-nowrap text-[var(--color-ink-3)] hover:text-[var(--color-ink)] ${i === 0 ? 'text-left' : 'text-right'} ${isActiveMetric ? 'text-[var(--color-copper)] bg-[var(--color-copper-soft)]' : ''}`}>
                  <span className="inline-flex items-center gap-1">
                    {i > 0 && <SortCaret active={isSorted} dir={sort.dir} />}
                    {c.label}
                    {i === 0 && <SortCaret active={isSorted} dir={sort.dir} />}
                  </span>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody className="tabular-nums font-[family-name:var(--font-mono)] text-[0.8rem]">
          {rows.map((r) => {
            const thin = isThin(r);
            return (
              <tr key={r.obec_id}
                className={`border-b border-[var(--color-rule-soft)] last:border-0 hover:bg-[var(--color-paper-2)] ${thin ? 'text-[var(--color-ink-3)]' : 'text-[var(--color-ink-2)]'}`}>
                {COLS.map((c, i) => (
                  <td key={c.key}
                    className={`px-3 py-1.5 whitespace-nowrap ${i === 0 ? 'text-left font-[family-name:var(--font-sans)] text-[var(--color-ink)]' : 'text-right'} ${c.metric === metric ? 'bg-[var(--color-copper-soft)]' : ''}`}>
                    {c.render(r)}
                    {i === 0 && thin && <span title="thin market" className="text-[var(--color-ink-4)]"> ·</span>}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function SortCaret({ active, dir }: { active: boolean; dir: 'asc' | 'desc' }) {
  return (
    <span className={`text-[0.6rem] ${active ? 'text-[var(--color-copper)]' : 'text-[var(--color-ink-4)]'}`}>
      {active ? (dir === 'asc' ? '▲' : '▼') : '▾'}
    </span>
  );
}

/* ---- filter chips ------------------------------------------------------- */

const COND: Record<string, string> = {
  '1': 'velmi dobrý', '2': 'dobrý', '6': 'novostavba', '8': 'před rekonstrukcí', '9': 'po rekonstrukci',
};
const CONSTR: Record<string, string> = { '5': 'panel', '2': 'cihla', '10': 'ostatní' };
const OWN: Record<string, string> = { '1': 'osobní', '2': 'družstevní', '3': 'státní' };

function FilterChips({ dataset, count }: { dataset: PriceStatDataset; count: number }) {
  const chips: string[] = [];
  if (dataset.building_condition) chips.push(COND[dataset.building_condition] ?? `stav ${dataset.building_condition}`);
  if (dataset.building_type) chips.push(CONSTR[dataset.building_type] ?? `konstr. ${dataset.building_type}`);
  if (dataset.ownership) chips.push(OWN[dataset.ownership] ?? `vl. ${dataset.ownership}`);
  if (dataset.usable_area_from != null || dataset.usable_area_to != null)
    chips.push(`${dataset.usable_area_from ?? 0}–${dataset.usable_area_to ?? '∞'} m²`);
  if (dataset.distance) chips.push(`okolí ${dataset.distance} km`);
  return (
    <div className="mt-3 flex flex-wrap items-center gap-1.5 text-xs text-[var(--color-ink-3)]">
      {chips.map((c) => (
        <span key={c} className="px-2 py-0.5 rounded-[var(--radius-xs)] border border-[var(--color-rule)] text-[var(--color-ink-2)]">{c}</span>
      ))}
      {count > 0 && <span className="ml-1">· {count} municipalities</span>}
    </div>
  );
}

/* ---- new dataset form --------------------------------------------------- */

const slugify = (s: string): string =>
  s.toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '').slice(0, 100);

const TYPE_OPTS: Array<[string, string]> = [['1', 'Byty'], ['2', 'Domy']];
const COND_OPTS: Array<[string, string]> = [['', 'Any condition'], ['1', 'Velmi dobrý'], ['6', 'Novostavba'], ['8', 'Před rekonstrukcí'], ['2', 'Dobrý'], ['9', 'Po rekonstrukci']];
const CONSTR_OPTS: Array<[string, string]> = [['', 'Any construction'], ['5', 'Panel'], ['2', 'Cihla'], ['10', 'Ostatní']];
const OWN_OPTS: Array<[string, string]> = [['', 'Any ownership'], ['1', 'Osobní'], ['2', 'Družstevní'], ['3', 'Státní']];
const PERIOD_OPTS: Array<[string, string]> = [['monthly', 'Monthly'], ['quarterly', 'Quarterly'], ['semiannual', 'Semiannual'], ['annual', 'Annual']];

function NewDatasetForm({ onClose, onCreated }: { onClose: () => void; onCreated: (d: PriceStatDataset) => void }) {
  const [name, setName] = useState('');
  const [categoryMain, setCategoryMain] = useState('1');
  const [condition, setCondition] = useState('');
  const [construction, setConstruction] = useState('');
  const [ownership, setOwnership] = useState('');
  const [areaFrom, setAreaFrom] = useState('');
  const [areaTo, setAreaTo] = useState('');
  const [obecIds, setObecIds] = useState<number[]>([]);
  const [minPop, setMinPop] = useState<number | null>(null);
  const [maxPop, setMaxPop] = useState<number | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [startYm, setStartYm] = useState(`${FIRST_YEAR}-01`);
  const [endYm, setEndYm] = useState(CUR_YM);
  const [periodicity, setPeriodicity] = useState('monthly');

  const mutation = useMutation({
    mutationFn: () => createPriceStatDataset({
      slug: slugify(name), name: name.trim(), category_main_cb: Number(categoryMain),
      building_condition: condition || null, building_type: construction || null,
      ownership: ownership || null,
      usable_area_from: areaFrom ? Number(areaFrom) : null,
      usable_area_to: areaTo ? Number(areaTo) : null,
      obec_ids: obecIds.length ? obecIds : null,
      min_population: minPop, max_population: maxPop,
      start_ym: startYm, end_ym: endYm,
      periodicity: periodicity as 'monthly' | 'quarterly' | 'semiannual' | 'annual',
    }),
    onSuccess: (d) => onCreated(d as PriceStatDataset),
  });
  const canSubmit = name.trim().length > 0 && slugify(name).length > 0 && !mutation.isPending;

  return (
    <form onSubmit={(e) => { e.preventDefault(); if (canSubmit) mutation.mutate(); }}
      className="mt-4 border border-[var(--color-rule)] rounded-[var(--radius-md)] bg-[var(--color-paper-2)] p-4">
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        <Field label="Name" className="lg:col-span-3">
          <input value={name} onChange={(e) => setName(e.target.value)}
            placeholder="Byty · novostavba · cihla · 60–120 m²" className={SELECT_CLS + ' w-full'} />
        </Field>
        <Field label="Type"><SelectBox value={categoryMain} onChange={setCategoryMain} options={TYPE_OPTS} /></Field>
        <Field label="Condition (stav)"><SelectBox value={condition} onChange={setCondition} options={COND_OPTS} /></Field>
        <Field label="Construction (konstrukce)"><SelectBox value={construction} onChange={setConstruction} options={CONSTR_OPTS} /></Field>
        <Field label="Ownership (vlastnictví)"><SelectBox value={ownership} onChange={setOwnership} options={OWN_OPTS} /></Field>
        <Field label="Area from (m²)"><input type="number" min={0} value={areaFrom} onChange={(e) => setAreaFrom(e.target.value)} className={SELECT_CLS + ' w-full'} /></Field>
        <Field label="Area to (m²)"><input type="number" min={0} value={areaTo} onChange={(e) => setAreaTo(e.target.value)} className={SELECT_CLS + ' w-full'} /></Field>
        <Field label="Municipalities">
          <button type="button" onClick={() => setPickerOpen(true)}
            className={SELECT_CLS + ' w-full text-left ' + (obecIds.length ? 'text-[var(--color-ink)]' : 'text-[var(--color-ink-3)]')}>
            {obecIds.length ? `${obecIds.length} selected` : 'All standard cities'}
          </button>
        </Field>
        <Field label="Scrape from">
          <YmPicker value={startYm} onChange={setStartYm} />
        </Field>
        <Field label="Scrape to">
          <YmPicker value={endYm} onChange={setEndYm} />
        </Field>
        <Field label="Periodicity">
          <SelectBox value={periodicity} onChange={setPeriodicity} options={PERIOD_OPTS} />
        </Field>
      </div>
      <div className="mt-3 flex items-center gap-2 text-xs text-[var(--color-ink-3)]">
        <span className="uppercase tracking-[0.14em]">Est. scrape time</span>
        <span className="text-[var(--color-ink-2)] tabular-nums">
          {estimateScrapeText(obecIds.length || DEFAULT_CITY_COUNT, startYm, endYm)}
        </span>
        <span>· {obecIds.length || DEFAULT_CITY_COUNT} municipalities</span>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <button type="submit" disabled={!canSubmit}
          className="text-sm rounded-[var(--radius-sm)] px-3 py-1.5 border border-[var(--color-copper)] text-[var(--color-copper)] hover:bg-[var(--color-copper-soft)] disabled:opacity-50">
          {mutation.isPending ? 'Creating…' : 'Create dataset'}
        </button>
        <button type="button" onClick={onClose} className="text-sm text-[var(--color-ink-3)] hover:text-[var(--color-ink)]">Cancel</button>
        {mutation.isError && <span className="text-sm text-[var(--color-brick)]">{(mutation.error as Error).message || 'Could not create dataset'}</span>}
      </div>
      <p className="mt-2 text-xs text-[var(--color-ink-3)]">
        Covers both prodej &amp; pronájem. Blank filter fields = no filter. Leave municipalities as
        “All standard cities” to use the default set. Populates on the next price-stats run.
      </p>

      {pickerOpen && (
        <CityPicker
          initialObecIds={obecIds} initialMin={minPop} initialMax={maxPop}
          onClose={() => setPickerOpen(false)}
          onApply={(ids, lo, hi) => { setObecIds(ids); setMinPop(lo); setMaxPop(hi); setPickerOpen(false); }}
        />
      )}
    </form>
  );
}

function Field({ label, className, children }: { label: string; className?: string; children: ReactNode }) {
  return (
    <label className={`block ${className ?? ''}`}>
      <span className="block mb-1 text-xs text-[var(--color-ink-3)]">{label}</span>
      {children}
    </label>
  );
}

function SelectBox({ value, onChange, options }: { value: string; onChange: (v: string) => void; options: Array<[string, string]> }) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)} className={SELECT_CLS + ' w-full'}>
      {options.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
    </select>
  );
}

/* ---- empty states ------------------------------------------------------- */

function EmptyDatasets() {
  return (
    <div className="mt-10 border border-dashed border-[var(--color-rule-strong)] rounded-[var(--radius-md)] p-8 text-center">
      <p className="text-sm text-[var(--color-ink-2)]">No datasets yet.</p>
      <p className="mt-1 text-xs text-[var(--color-ink-3)]">Create one with “+ New dataset”; it populates on the next price-stats run.</p>
    </div>
  );
}

function EmptyData({ run }: { run: PriceStatRun | null }) {
  const running = run?.status === 'running';
  const failed = run?.status === 'failed';
  return (
    <div className="border border-dashed border-[var(--color-rule-strong)] rounded-[var(--radius-md)] p-8 text-center">
      <p className="text-sm text-[var(--color-ink-2)]">
        {running ? 'Scraping in progress — data will appear here when it lands.'
          : failed ? 'No data — the last scrape failed (see above).'
          : 'No data for this dataset / window yet.'}
      </p>
      {!running && !failed && (
        <p className="mt-1 text-xs text-[var(--color-ink-3)]">
          It populates on the next <code>scrape_price_stats</code> run.
        </p>
      )}
    </div>
  );
}

function RunStatusBanner({ run }: { run: PriceStatRun }) {
  if (run.status === 'running') {
    const pct = run.cities_total > 0 ? Math.round((run.cities_done / run.cities_total) * 100) : 0;
    return (
      <div className="mt-4 border border-[var(--color-copper)] bg-[var(--color-copper-soft)] rounded-[var(--radius-md)] px-4 py-3">
        <div className="flex items-center justify-between text-sm">
          <span className="text-[var(--color-ink)]">
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-[var(--color-copper)] mr-2 animate-pulse" />
            Scraping… <span className="tabular-nums">{run.cities_done}</span> / <span className="tabular-nums">{run.cities_total || '?'}</span> municipalities
          </span>
          <span className="text-xs tabular-nums text-[var(--color-ink-3)]">
            {run.observations.toLocaleString('cs-CZ')} data points
          </span>
        </div>
        <div className="mt-2 h-1.5 rounded-full bg-[var(--color-paper-3)] overflow-hidden">
          <div className="h-full bg-[var(--color-copper)] transition-[width] duration-500" style={{ width: `${pct}%` }} />
        </div>
      </div>
    );
  }
  if (run.status === 'failed') {
    return (
      <div className="mt-4 border border-[var(--color-brick)] bg-[var(--color-brick-soft)] rounded-[var(--radius-md)] px-4 py-3 text-sm text-[var(--color-ink-2)]">
        Last scrape failed{run.error ? `: ${run.error.slice(0, 200)}` : '.'}
      </div>
    );
  }
  return null;
}
