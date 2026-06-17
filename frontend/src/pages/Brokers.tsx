import { useMemo, type ReactNode } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import {
  fetchBrokerGeoOptions,
  fetchBrokerLeaderboard,
  prettyPhone,
  type BrokerLeaderRow,
  type LeaderMetric,
} from '../lib/brokers';
import { PickButton } from '../components/controls';
import { fmtCount } from '../lib/format';

// Page-local UI vocabulary — there is no shared category_main/type label map, and
// these are the Brokers page's own filter words.
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

const SELECT_CLS =
  'text-sm border border-[var(--color-rule)] rounded-[var(--radius-sm)] ' +
  'bg-[var(--color-paper-3)] px-2.5 py-1.5 text-[var(--color-ink)] ' +
  'focus:outline-none focus:ring-2 focus:ring-[var(--color-focus)]';

export default function Brokers() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();

  const geoQ = useQuery({
    queryKey: ['broker-geo-options'],
    queryFn: fetchBrokerGeoOptions,
    staleTime: 5 * 60_000,
  });

  const regions = useMemo(
    () =>
      (geoQ.data ?? [])
        .filter((o) => o.geo_level === 'region')
        .sort((a, b) => b.broker_count - a.broker_count),
    [geoQ.data],
  );

  // Region defaults to the busiest kraj once options load; URL is the source of truth.
  const krajParam = params.get('kraj');
  const kraj = krajParam ? Number(krajParam) : regions[0]?.geo_id ?? null;
  const okres = params.get('okres') ? Number(params.get('okres')) : null;
  const categoryMain = params.has('cm') ? params.get('cm') || null : 'byt';
  const categoryType = params.has('ct') ? params.get('ct') || null : 'prodej';
  const metric = (params.get('metric') as LeaderMetric) || 'active_property_count';

  const okresOptions = useMemo(
    () =>
      (geoQ.data ?? [])
        .filter((o) => o.geo_level === 'okres' && o.parent_id === kraj)
        .sort((a, b) => a.name.localeCompare(b.name, 'cs')),
    [geoQ.data, kraj],
  );

  const geoLevel = okres ? 'okres' : 'region';
  const geoId = okres ?? kraj;

  const boardQ = useQuery({
    queryKey: ['broker-leaderboard', geoLevel, geoId, categoryMain, categoryType, metric],
    queryFn: () =>
      fetchBrokerLeaderboard({
        geoLevel,
        geoId: geoId as number,
        categoryMain,
        categoryType,
        metric,
        limit: 150,
      }),
    enabled: geoId != null,
    staleTime: 60_000,
  });

  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(params);
    if (value === null) next.delete(key);
    else next.set(key, value);
    setParams(next, { replace: true });
  };

  const rows = boardQ.data ?? [];
  const krajName = regions.find((r) => r.geo_id === kraj)?.name;
  const okresName = okresOptions.find((o) => o.geo_id === okres)?.name;
  const placeLabel = okresName ?? krajName ?? '';

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

      {/* Filter ledger header */}
      <div className="mt-6 border border-[var(--color-rule)] rounded-[var(--radius-md)] bg-[var(--color-paper-2)] px-4 py-3.5 flex flex-wrap items-end gap-x-6 gap-y-3">
        <Field label="Kraj">
          <select
            className={SELECT_CLS}
            value={kraj ?? ''}
            onChange={(e) => {
              setParam('kraj', e.target.value || null);
              setParam('okres', null);
            }}
          >
            {regions.map((r) => (
              <option key={r.geo_id} value={r.geo_id}>
                {r.name} · {r.broker_count}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Okres">
          <select
            className={SELECT_CLS}
            value={okres ?? ''}
            onChange={(e) => setParam('okres', e.target.value || null)}
            disabled={okresOptions.length === 0}
          >
            <option value="">— celý kraj —</option>
            {okresOptions.map((o) => (
              <option key={o.geo_id} value={o.geo_id}>
                {o.name} · {o.broker_count}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Typ">
          <Segmented
            options={CATEGORY_OPTIONS}
            value={categoryMain}
            onChange={(v) => setParam('cm', v === 'byt' ? null : v ?? '')}
          />
        </Field>
        <Field label="Nabídka">
          <Segmented
            options={OFFER_OPTIONS}
            value={categoryType}
            onChange={(v) => setParam('ct', v === 'prodej' ? null : v ?? '')}
          />
        </Field>
        <Field label="Řadit dle">
          <Segmented
            options={METRIC_OPTIONS}
            value={metric}
            onChange={(v) => setParam('metric', v === 'active_property_count' ? null : v)}
          />
        </Field>
      </div>

      {/* The ledger */}
      <div className="mt-5">
        {boardQ.isLoading || geoQ.isLoading ? (
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
            onOpen={(id) => navigate(`/brokers/${id}`)}
          />
        )}
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
        {label}
      </span>
      {children}
    </label>
  );
}

function Segmented<T extends string | null>({
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
        <PickButton
          key={o.label}
          on={value === o.value}
          onClick={() => onChange(o.value)}
        >
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
  onOpen,
}: {
  rows: BrokerLeaderRow[];
  metric: LeaderMetric;
  placeLabel: string;
  onOpen: (brokerId: number) => void;
}) {
  return (
    <div className="border border-[var(--color-rule)] rounded-[var(--radius-md)] overflow-hidden">
      <div className="px-4 py-2.5 border-b border-[var(--color-rule)] bg-[var(--color-paper-2)] flex items-baseline justify-between">
        <span className="text-xs tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
          {placeLabel}
        </span>
        <span className="text-[0.7rem] text-[var(--color-ink-4)] tabular-nums">
          {rows.length} makléřů
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
        Žádní makléři pro tento výběr{placeLabel ? ` v ${placeLabel}` : ''}.
      </p>
      <p className="mt-1 text-xs text-[var(--color-ink-3)]">
        Zkuste jiný typ nemovitosti nebo nabídku.
      </p>
    </div>
  );
}
