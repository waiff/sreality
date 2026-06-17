import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import {
  chipsToGeoArrays,
  fetchBrokerLeaderboard,
  prettyPhone,
  searchBrokersByName,
  type BrokerLeaderRow,
  type LeaderMetric,
} from '../lib/brokers';
import type { DistrictChip } from '../lib/filters';
import { LocationTypeahead } from '../components/filter-controls/LocationTypeahead';
import { PickButton } from '../components/controls';
import { fmtCount } from '../lib/format';

const CATEGORY_OPTIONS: ReadonlyArray<{ value: string | null; label: string }> = [
  { value: 'byt', label: 'Byty' },
  { value: 'dum', label: 'Domy' },
  { value: 'pozemek', label: 'Pozemky' },
  { value: 'komercni', label: 'Komerční' },
  { value: null, label: 'Vše' },
];
const OFFER_OPTIONS: ReadonlyArray<{ value: string | null; label: string }> = [
  { value: 'prodej', label: 'Prodej' },
  { value: 'pronajem', label: 'Pronájem' },
  { value: null, label: 'Vše' },
];
const METRIC_OPTIONS: ReadonlyArray<{ value: LeaderMetric; label: string }> = [
  { value: 'active_property_count', label: 'Nemovitosti' },
  { value: 'listing_count', label: 'Inzeráty' },
];
const LIMIT_OPTIONS: ReadonlyArray<{ value: number; label: string }> = [
  { value: 50, label: '50' },
  { value: 100, label: '100' },
  { value: 200, label: '200' },
  { value: 2000, label: 'Vše' },
];

export default function Brokers() {
  const navigate = useNavigate();
  const [districts, setDistricts] = useState<DistrictChip[]>([]);
  const [categoryMain, setCategoryMain] = useState<string | null>('byt');
  const [categoryType, setCategoryType] = useState<string | null>('prodej');
  const [metric, setMetric] = useState<LeaderMetric>('active_property_count');
  const [limit, setLimit] = useState<number>(100);

  const geo = useMemo(() => chipsToGeoArrays(districts), [districts]);

  const boardQ = useQuery({
    queryKey: [
      'broker-leaderboard',
      geo.regionIds, geo.okresIds, geo.obecIds,
      categoryMain, categoryType, metric, limit,
    ],
    queryFn: () =>
      fetchBrokerLeaderboard({ ...geo, categoryMain, categoryType, metric, limit }),
    staleTime: 60_000,
  });

  const rows = boardQ.data ?? [];
  const resolved = districts.filter((d) => d.id != null && !d.excluded);
  const placeLabel =
    resolved.length === 0 ? 'Celá ČR' : resolved.map((d) => d.name).join(' + ');

  return (
    <div className="px-6 py-8 max-w-5xl mx-auto text-[var(--color-ink)]">
      <header>
        <p className="text-xs tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          broker intelligence
        </p>
        <h1 className="mt-1 text-2xl font-[family-name:var(--font-display)]">Makléři</h1>
        <p className="mt-1 text-sm text-[var(--color-ink-3)] max-w-2xl">
          Kdo drží nejvíc inventáře v daném regionu a typu nemovitosti — žebříček
          makléřů a jejich kontakty pro oslovení.
        </p>
      </header>

      <NameSearch onPick={(id) => navigate(`/brokers/${id}`)} />

      {/* Filter ledger header */}
      <div className="mt-5 border border-[var(--color-rule)] rounded-[var(--radius-md)] bg-[var(--color-paper-2)] px-4 py-3.5 flex flex-wrap items-end gap-x-6 gap-y-3">
        <Field label="Lokalita" className="min-w-[16rem] flex-1">
          <LocationTypeahead
            value={districts}
            onChange={(next) => setDistricts(next ?? [])}
          />
        </Field>
        <Field label="Typ">
          <Segmented options={CATEGORY_OPTIONS} value={categoryMain} onChange={setCategoryMain} />
        </Field>
        <Field label="Nabídka">
          <Segmented options={OFFER_OPTIONS} value={categoryType} onChange={setCategoryType} />
        </Field>
        <Field label="Řadit dle">
          <Segmented options={METRIC_OPTIONS} value={metric} onChange={setMetric} />
        </Field>
        <Field label="Počet">
          <Segmented options={LIMIT_OPTIONS} value={limit} onChange={setLimit} />
        </Field>
      </div>

      {/* The ledger */}
      <div className="mt-5">
        {boardQ.isLoading ? (
          <p className="mt-10 text-sm text-[var(--color-ink-3)]">Načítám žebříček…</p>
        ) : boardQ.isError ? (
          <p className="mt-4 text-sm text-[var(--color-brick)]">
            {(boardQ.error as Error).message}
          </p>
        ) : rows.length === 0 ? (
          <Empty placeLabel={placeLabel} />
        ) : (
          <Ledger
            rows={rows}
            metric={metric}
            placeLabel={placeLabel}
            capped={rows.length >= limit}
            onOpen={(id) => navigate(`/brokers/${id}`)}
          />
        )}
      </div>
    </div>
  );
}

function NameSearch({ onPick }: { onPick: (brokerId: number) => void }) {
  const [q, setQ] = useState('');
  const [debounced, setDebounced] = useState('');
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(q.trim()), 200);
    return () => clearTimeout(t);
  }, [q]);

  const resultsQ = useQuery({
    queryKey: ['broker-name-search', debounced],
    queryFn: () => searchBrokersByName(debounced),
    enabled: debounced.length >= 2,
    staleTime: 60_000,
  });
  const results = resultsQ.data ?? [];

  return (
    <div className="mt-5 relative max-w-xl">
      <input
        value={q}
        onChange={(e) => {
          setQ(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        placeholder="Hledat makléře podle jména…"
        className="w-full text-sm border border-[var(--color-rule)] rounded-[var(--radius-sm)] bg-[var(--color-paper-3)] px-3 py-2 text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:ring-2 focus:ring-[var(--color-focus)]"
      />
      {open && debounced.length >= 2 && (
        <div className="absolute z-20 mt-1 w-full border border-[var(--color-rule)] rounded-[var(--radius-md)] bg-[var(--color-paper-3)] shadow-sm max-h-80 overflow-y-auto">
          {resultsQ.isLoading ? (
            <p className="px-3 py-2 text-sm text-[var(--color-ink-3)]">Hledám…</p>
          ) : results.length === 0 ? (
            <p className="px-3 py-2 text-sm text-[var(--color-ink-4)]">Nic nenalezeno.</p>
          ) : (
            results.map((b) => (
              <button
                key={b.broker_id}
                type="button"
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => onPick(b.broker_id)}
                className="w-full text-left px-3 py-2 flex items-center justify-between gap-3 border-b border-[var(--color-rule-soft)] last:border-0 hover:bg-[var(--color-copper-soft)]"
              >
                <span className="min-w-0">
                  <span className="block truncate text-sm text-[var(--color-ink)]">
                    {b.display_name ?? 'Neznámý makléř'}
                  </span>
                  <span className="block truncate text-xs text-[var(--color-ink-3)]">
                    {b.firm_name ?? b.firm_domain ?? 'nezávislý'}
                  </span>
                </span>
                <span className="shrink-0 text-xs font-[family-name:var(--font-mono)] tabular-nums text-[var(--color-ink-3)]">
                  {fmtCount(b.active_property_count)}
                </span>
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}

function Field({
  label,
  children,
  className = '',
}: {
  label: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <label className={`flex flex-col gap-1 ${className}`}>
      <span className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
        {label}
      </span>
      {children}
    </label>
  );
}

function Segmented<T extends string | number | null>({
  options,
  value,
  onChange,
}: {
  options: ReadonlyArray<{ value: T; label: string }>;
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div className="flex flex-wrap gap-1">
      {options.map((o) => (
        <PickButton key={o.label} on={value === o.value} onClick={() => onChange(o.value)}>
          {o.label}
        </PickButton>
      ))}
    </div>
  );
}

function Ledger({
  rows,
  metric,
  placeLabel,
  capped,
  onOpen,
}: {
  rows: BrokerLeaderRow[];
  metric: LeaderMetric;
  placeLabel: string;
  capped: boolean;
  onOpen: (brokerId: number) => void;
}) {
  return (
    <div className="border border-[var(--color-rule)] rounded-[var(--radius-md)] overflow-hidden">
      <div className="px-4 py-2.5 border-b border-[var(--color-rule)] bg-[var(--color-paper-2)] flex items-baseline justify-between">
        <span className="text-xs tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
          {placeLabel}
        </span>
        <span className="text-[0.7rem] text-[var(--color-ink-4)] tabular-nums">
          {rows.length} makléřů{capped ? ' (limit)' : ''}
        </span>
      </div>
      <ol>
        {rows.map((r, i) => (
          <li key={r.broker_id}>
            <button
              type="button"
              onClick={() => onOpen(r.broker_id)}
              className="w-full text-left px-4 py-3 flex items-center gap-4 border-b border-[var(--color-rule-soft)] last:border-0 hover:bg-[var(--color-copper-soft)] transition-colors group"
            >
              <span className="w-7 shrink-0 text-right font-[family-name:var(--font-display)] text-lg text-[var(--color-copper)] tabular-nums">
                {i + 1}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate font-[family-name:var(--font-display)] text-[0.98rem] text-[var(--color-ink)] group-hover:text-[var(--color-copper-2)]">
                  {r.display_name ?? 'Neznámý makléř'}
                </span>
                <span className="block truncate text-xs text-[var(--color-ink-3)] mt-0.5">
                  {r.firm_name ?? r.firm_domain ?? 'nezávislý / neznámá kancelář'}
                </span>
              </span>
              <span className="hidden sm:block w-40 shrink-0 text-xs font-[family-name:var(--font-mono)] text-[var(--color-ink-2)] tabular-nums">
                {r.primary_phone ? prettyPhone(r.primary_phone) : '—'}
              </span>
              <Count
                value={r.active_property_count}
                total={r.property_count}
                label="nemovitostí"
                emphasized={metric === 'active_property_count' || metric === 'property_count'}
              />
              <Count
                value={r.active_listing_count}
                total={r.listing_count}
                label="inzerátů"
                emphasized={metric === 'listing_count' || metric === 'active_listing_count'}
              />
            </button>
          </li>
        ))}
      </ol>
    </div>
  );
}

function Count({
  value,
  total,
  label,
  emphasized,
}: {
  value: number;
  total: number;
  label: string;
  emphasized: boolean;
}) {
  return (
    <span className="w-24 shrink-0 text-right">
      <span
        className={[
          'block font-[family-name:var(--font-mono)] tabular-nums',
          emphasized ? 'text-base text-[var(--color-ink)]' : 'text-sm text-[var(--color-ink-3)]',
        ].join(' ')}
      >
        {fmtCount(value)}
        {total > value && (
          <span className="text-[var(--color-ink-4)] text-xs"> / {fmtCount(total)}</span>
        )}
      </span>
      <span className="block text-[0.6rem] tracking-[0.1em] uppercase text-[var(--color-ink-4)] mt-0.5">
        {label}
      </span>
    </span>
  );
}

function Empty({ placeLabel }: { placeLabel: string }) {
  return (
    <div className="mt-6 border border-dashed border-[var(--color-rule-strong)] rounded-[var(--radius-md)] p-8 text-center">
      <p className="text-sm text-[var(--color-ink-2)]">
        Žádní makléři pro tento výběr v {placeLabel}.
      </p>
      <p className="mt-1 text-xs text-[var(--color-ink-3)]">
        Zkuste jiný typ nemovitosti nebo nabídku.
      </p>
    </div>
  );
}
