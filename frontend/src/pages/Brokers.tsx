import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { listBrokerMergeCandidates } from '../lib/api';
import { useAuth } from '@/lib/auth';
import { isSupabaseConfigured } from '@/lib/supabase';
import {
  chipsToGeoArrays,
  contactState,
  fetchBrokerLeaderboard,
  prettyPhone,
  searchBrokerFirms,
  searchBrokersByName,
  type BrokerFirmOption,
  type BrokerLeaderRow,
  type LeaderMetric,
} from '../lib/brokers';
import type { DistrictChip } from '../lib/filters';
import { LocationTypeahead } from '../components/filter-controls/LocationTypeahead';
import { Field, PickButton, Segmented, Switch } from '../components/controls';
import { BufferedNumberInput } from '../components/FilterForm';
import { SUBTYPE_LABELS_BY_MAIN } from '@/lib/enums';
import { ROUTES } from '@/lib/routes';
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
  const [districts, setDistricts] = useState<DistrictChip[]>([]);
  const [firmIds, setFirmIds] = useState<number[]>([]);
  const [categoryMain, setCategoryMain] = useState<string | null>('byt');
  const [categoryType, setCategoryType] = useState<string | null>('prodej');
  const [metric, setMetric] = useState<LeaderMetric>('active_property_count');
  const [limit, setLimit] = useState<number>(100);
  const [minPriceCzk, setMinPriceCzk] = useState<number | null>(null);
  const [includeUnpriced, setIncludeUnpriced] = useState(false);
  const [subtypes, setSubtypes] = useState<string[]>([]);
  const [includeUnknownSubtype, setIncludeUnknownSubtype] = useState(false);

  // Subtype is only populated for houses and commercial (the registry's own dum /
  // komercni partition), so the control only exists for those two — same rule Browse
  // applies in its sidebar, from the same SUBTYPE_LABELS_BY_MAIN vocabulary.
  const subtypeOptions =
    categoryMain === 'dum' || categoryMain === 'komercni'
      ? SUBTYPE_LABELS_BY_MAIN[categoryMain]
      : null;

  // Clear the subtype filter whenever Typ actually changes: the slugs are partitioned
  // by category_main, so a house subtype carried into Komerční matches nothing and
  // would silently empty the ledger (Browse clears its own subtype filter for exactly
  // this reason). Guarded so re-clicking the selected pill is a no-op.
  const handleCategoryMainChange = (next: string | null) => {
    if (next !== categoryMain) {
      setSubtypes([]);
      setIncludeUnknownSubtype(false);
    }
    setCategoryMain(next);
  };

  const toggleSubtype = (slug: string) => {
    setSubtypes((prev) =>
      prev.includes(slug) ? prev.filter((s) => s !== slug) : [...prev, slug],
    );
  };

  // price_czk means something different per offer type (total price for a sale,
  // monthly rent for a rental — same dual meaning Browse's own price filter has), so
  // a value typed for one scale is meaningless on another: "Vše" mixes all four
  // offer types under one number, and even Prodej<->Pronájem directly would silently
  // reinterpret e.g. "5000000" from a sale price into an absurd monthly rent. Clear on
  // any ACTUAL change (guarded so re-clicking the already-selected pill is a no-op).
  const handleCategoryTypeChange = (next: string | null) => {
    if (next !== categoryType) {
      setMinPriceCzk(null);
      setIncludeUnpriced(false);
    }
    setCategoryType(next);
  };

  const geo = useMemo(() => chipsToGeoArrays(districts), [districts]);

  // reason_counts is the whole queue; `count` is only the page, so the badge used
  // to pin at its own 100-row limit (and paid for 100 enriched rows to show one
  // number). Ask for a single row and read the counts.
  //
  // /broker-review/candidates is require_admin while this page is only agenda-
  // gated, so an ungated query is two guaranteed 403s per non-admin page view
  // (retry: 1, and react-query refetches an errored query on every remount) —
  // each one opening a Postgres connection before the auth check runs. The badge
  // links to /brokers/review, which a non-admin cannot open anyway. The
  // `!isSupabaseConfigured()` arm mirrors Shell's `showAdmin` and guards.tsx so
  // the badge isn't the one admin affordance that stays dark in local dev.
  const { isAdmin } = useAuth();
  const canReview = isAdmin || !isSupabaseConfigured();
  const reviewQ = useQuery({
    queryKey: ['broker-merge-candidates-count'],
    queryFn: () => listBrokerMergeCandidates(1),
    staleTime: 300_000,
    enabled: canReview,
  });
  const reviewCount = Object.values(reviewQ.data?.reason_counts ?? {})
    .reduce((a, b) => a + b, 0);

  const boardQ = useQuery({
    queryKey: [
      'broker-leaderboard',
      geo.regionIds, geo.okresIds, geo.obecIds,
      categoryMain, categoryType, metric, limit, firmIds, minPriceCzk, includeUnpriced,
      subtypes, includeUnknownSubtype,
    ],
    queryFn: () =>
      fetchBrokerLeaderboard({
        ...geo, categoryMain, categoryType, metric, limit, firmIds,
        minPriceCzk, includeUnpriced, subtypes, includeUnknownSubtype,
      }),
    staleTime: 60_000,
    // Every filter control (region, type, metric, firm) changes this query's
    // key. Without this, each click blanked the whole ledger back to
    // "Načítám žebříček…" and re-painted from scratch instead of updating in
    // place — keep showing the last result while the new one loads.
    placeholderData: keepPreviousData,
  });

  const rows = boardQ.data ?? [];
  const resolved = districts.filter((d) => d.id != null && !d.excluded);
  const placeLabel =
    resolved.length === 0 ? 'Celá ČR' : resolved.map((d) => d.name).join(' + ');

  return (
    <div className="px-6 py-8 max-w-5xl mx-auto text-[var(--color-ink)]">
      <header className="flex items-start justify-between gap-4">
        <div>
          <p className="text-xs tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
            broker intelligence
          </p>
          <h1 className="mt-1 text-2xl font-[family-name:var(--font-display)]">Makléři</h1>
          <p className="mt-1 text-sm text-[var(--color-ink-3)] max-w-2xl">
            Kdo drží nejvíc inventáře v daném regionu a typu nemovitosti — žebříček
            makléřů a jejich kontakty pro oslovení.
          </p>
        </div>
        {reviewCount > 0 && (
          <Link to={ROUTES.brokersReview.build()}
            className="shrink-0 mt-1 text-xs rounded-[var(--radius-sm)] border border-[var(--color-copper)] bg-[var(--color-copper-soft)] px-3 py-1.5 text-[var(--color-copper)] hover:bg-[var(--color-copper)] hover:text-[var(--color-paper)] transition-colors">
            Sloučit duplicity ({reviewCount})
          </Link>
        )}
      </header>

      <NameSearch />

      {/* Filter ledger header. Firma sits on its own row, full width: it can
          run to dozens of pills wrapping across several lines, and sharing a
          row with the single-line controls made THEIR flex-wrap basis follow
          Firma's unwrapped content width instead of their own. */}
      <div className="mt-5 border border-[var(--color-rule)] rounded-[var(--radius-md)] bg-[var(--color-paper-2)] px-4 py-3.5">
        <div className="flex flex-wrap items-end gap-x-6 gap-y-3">
          <Field label="Lokalita" className="min-w-[16rem] flex-1">
            <LocationTypeahead
              value={districts}
              onChange={(next) => setDistricts(next ?? [])}
            />
          </Field>
          <Field label="Typ">
            <Segmented options={CATEGORY_OPTIONS} value={categoryMain} onChange={handleCategoryMainChange} />
          </Field>
          <Field label="Nabídka">
            <Segmented options={OFFER_OPTIONS} value={categoryType} onChange={handleCategoryTypeChange} />
          </Field>
          <Field label="Min. cena">
            {categoryType == null ? (
              <p className="text-xs text-[var(--color-ink-4)] py-1.5 max-w-[9rem]">
                Zvolte Prodej nebo Pronájem
              </p>
            ) : (
              <div className="flex items-center gap-2">
                <div className="w-28">
                  <BufferedNumberInput
                    value={minPriceCzk}
                    coerce="int"
                    placeholder={categoryType === 'pronajem' ? 'Kč/měsíc' : 'Kč'}
                    onChange={setMinPriceCzk}
                    ariaLabel="Minimální cena"
                  />
                </div>
                <label
                  className="flex items-center gap-1.5 text-xs text-[var(--color-ink-3)] cursor-pointer select-none"
                  title="Počítat i nabídky s cenou na vyžádání, jako by minimální cenu splňovaly."
                >
                  <Switch
                    on={includeUnpriced}
                    onChange={setIncludeUnpriced}
                    disabled={minPriceCzk == null}
                    ariaLabel="Počítat nabídky bez ceny"
                  />
                  bez ceny
                </label>
              </div>
            )}
          </Field>
          <Field label="Řadit dle">
            <Segmented options={METRIC_OPTIONS} value={metric} onChange={setMetric} />
          </Field>
          <Field label="Počet">
            <Segmented options={LIMIT_OPTIONS} value={limit} onChange={setLimit} />
          </Field>
        </div>
        {/* Podtyp sits on its own row for the same reason Firma does: up to twelve
            pills that wrap across lines would otherwise drive the single-line
            controls' flex-wrap basis. Only rendered for dum / komercni — the two
            category_main values the registry defines subtypes for. */}
        {subtypeOptions && (
          <div className="mt-3.5 pt-3.5 border-t border-[var(--color-rule-soft)]">
            <Field label="Podtyp">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                <div className="flex flex-wrap gap-1">
                  {subtypeOptions.map((o) => (
                    <PickButton
                      key={o.slug}
                      on={subtypes.includes(o.slug)}
                      onClick={() => toggleSubtype(o.slug)}
                    >
                      {o.label}
                    </PickButton>
                  ))}
                </div>
                <label
                  className="flex items-center gap-1.5 text-xs text-[var(--color-ink-3)] cursor-pointer select-none"
                  title="Počítat i nabídky bez uvedeného podtypu, jako by vybraný podtyp splňovaly."
                >
                  <Switch
                    on={includeUnknownSubtype}
                    onChange={setIncludeUnknownSubtype}
                    disabled={subtypes.length === 0}
                    ariaLabel="Počítat nabídky bez podtypu"
                  />
                  bez podtypu
                </label>
              </div>
            </Field>
            {subtypes.length > 0 && <SubtypeCoverageNote />}
          </div>
        )}
        <div className="mt-3.5 pt-3.5 border-t border-[var(--color-rule-soft)]">
          <Field label="Firma">
            <CompanyFilter value={firmIds} onChange={setFirmIds} />
          </Field>
        </div>
      </div>

      {/* The ledger. boardQ keeps the previous page's rows visible while a
          filter change loads the next one (placeholderData: keepPreviousData
          above) — isFetching (not isLoading) is the "still updating" signal
          once there's something on screen to update. */}
      <div className="mt-5">
        {boardQ.isLoading ? (
          <p className="mt-10 text-sm text-[var(--color-ink-3)]">Načítám žebříček…</p>
        ) : boardQ.isError ? (
          <p className="mt-4 text-sm text-[var(--color-brick)]">
            {(boardQ.error as Error).message}
          </p>
        ) : rows.length === 0 ? (
          <Empty
            placeLabel={placeLabel}
            hasFirmFilter={firmIds.length > 0}
            hasValueFilter={minPriceCzk != null}
            hasSubtypeFilter={subtypes.length > 0}
          />
        ) : (
          <>
            {boardQ.isFetching && (
              <p className="mb-2 text-xs text-[var(--color-ink-3)]">Aktualizuji…</p>
            )}
            <Ledger
              rows={rows}
              metric={metric}
              placeLabel={placeLabel}
              capped={rows.length >= limit}
            />
          </>
        )}
      </div>
    </div>
  );
}

// Shared by NameSearch and CompanyFilter — both debounce a free-text query
// the same way, only the dropdown/pick behavior after it differs.
function useDebouncedTerm(delayMs = 200): [string, string, (next: string) => void] {
  const [q, setQ] = useState('');
  const [debounced, setDebounced] = useState('');
  useEffect(() => {
    const t = setTimeout(() => setDebounced(q.trim()), delayMs);
    return () => clearTimeout(t);
  }, [q, delayMs]);
  return [q, debounced, setQ];
}

function NameSearch() {
  const [q, debounced, setQ] = useDebouncedTerm();
  const [open, setOpen] = useState(false);

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
          ) : resultsQ.isError ? (
            /* A failed search is not an empty one — "Nic nenalezeno." for both is
               exactly how the dark PostgREST reads stayed invisible for a month. */
            <p className="px-3 py-2 text-sm text-[var(--color-brick)]">
              Hledání selhalo: {(resultsQ.error as Error).message}
            </p>
          ) : results.length === 0 ? (
            <p className="px-3 py-2 text-sm text-[var(--color-ink-4)]">Nic nenalezeno.</p>
          ) : (
            results.map((b) => (
              /* onMouseDown-preventDefault keeps the input's onBlur from
                 closing the list before the click lands; it does NOT suppress
                 the anchor's own navigation, which fires on click. */
              <Link
                key={b.broker_id}
                to={ROUTES.brokerDetail.build({ id: b.broker_id })}
                onMouseDown={(e) => e.preventDefault()}
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
                {/* The CZ-scoped count is what the list is ordered by (migration
                    396), so it has to be the number shown; the whole book stays
                    visible as the dimmed total whenever the broker also carries
                    foreign or ungeocoded stock. */}
                <span className="shrink-0 text-xs font-[family-name:var(--font-mono)] tabular-nums text-[var(--color-ink-3)]">
                  {fmtCount(b.cz_active_property_count)}
                  {b.active_property_count > b.cz_active_property_count && (
                    <span className="text-[var(--color-ink-4)]"> / {fmtCount(b.active_property_count)}</span>
                  )}
                </span>
              </Link>
            ))
          )}
        </div>
      )}
    </div>
  );
}

// display_name is NULL for every franchise domain (mmreality.cz, re-max.cz, ...)
// and any domain under the resolver's 60% modal-label share — the same fallback
// the leaderboard row itself uses for firm_name ?? firm_domain.
function firmLabel(f: BrokerFirmOption): string {
  return f.display_name ?? f.canonical_domain ?? 'neznámá firma';
}

// The busiest companies as toggleable pills, same PickButton language as Typ/
// Nabídka but multi-select — searchBrokerFirms('') is the same "browse top
// firms by broker_count" path the old dropdown's empty state used, just as
// the whole picker now instead of a step before typing.
function CompanyFilter({
  value,
  onChange,
}: {
  value: number[];
  onChange: (next: number[]) => void;
}) {
  const optionsQ = useQuery({
    queryKey: ['broker-firm-options', ''],
    queryFn: () => searchBrokerFirms('', 24),
    staleTime: 60_000,
  });
  const options = optionsQ.data ?? [];
  const selected = new Set(value);

  const toggle = (firmId: number) => {
    if (selected.has(firmId)) onChange(value.filter((id) => id !== firmId));
    else onChange([...value, firmId]);
  };

  if (optionsQ.isLoading) {
    return <p className="text-sm text-[var(--color-ink-3)]">Načítám firmy…</p>;
  }
  if (optionsQ.isError) {
    return <p className="text-sm text-[var(--color-brick)]">Firmy se nepodařilo načíst.</p>;
  }

  return (
    <div className="flex flex-wrap gap-1">
      {options.map((f) => (
        <PickButton key={f.firm_id} on={selected.has(f.firm_id)} onClick={() => toggle(f.firm_id)}>
          {firmLabel(f)}{' '}
          <span className="font-[family-name:var(--font-mono)] tabular-nums opacity-70">
            ({fmtCount(f.broker_count)})
          </span>
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
}: {
  rows: BrokerLeaderRow[];
  metric: LeaderMetric;
  placeLabel: string;
  capped: boolean;
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
            {/* A real anchor, not a button with a navigate() handler: an analyst
              * comparing brokers needs right-click → open in a new tab,
              * ctrl/middle-click, and a copyable link address. Every child here
              * is a <span>, so there is no nested-interactive problem. */}
            <Link
              to={ROUTES.brokerDetail.build({ id: r.broker_id })}
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
              <PhoneCell row={r} />
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
            </Link>
          </li>
        ))}
      </ol>
    </div>
  );
}

/* The API hands a non-admin `has_phone` instead of the number itself, so an
   em-dash here would read as "this broker has no phone on file". */
function PhoneCell({ row }: { row: BrokerLeaderRow }) {
  const phone = contactState(row.primary_phone, row.has_phone);
  return (
    <span
      className="hidden sm:block w-40 shrink-0 text-xs font-[family-name:var(--font-mono)] text-[var(--color-ink-2)] tabular-nums"
      title={phone.state === 'masked' ? 'Kontakt je viditelný jen pro administrátory.' : undefined}
    >
      {phone.state === 'value' ? (
        prettyPhone(phone.value)
      ) : phone.state === 'masked' ? (
        <span className="text-[var(--color-ink-4)]">na vyžádání</span>
      ) : (
        '—'
      )}
    </span>
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

/* Subtype coverage is a PORTAL gap, not random sparsity — measured on active,
   attributed, CZ-resolved rows: sreality labels 100% of houses and commercial,
   idnes most of them, while ceskereality, realitymix and mmreality label NONE and
   remax almost none. Half of all active houses carry no subtype at all. Because
   brokers cluster by portal that bias is not noise: two of the top fifteen brokers
   by unfiltered commercial inventory have zero labelled rows and drop out of a
   subtype-filtered ranking entirely. Stating it inline is the point — a silently
   portal-biased leaderboard would be worse than no filter, and the "bez podtypu"
   switch is the lever that puts the unlabelled rows back. */
function SubtypeCoverageNote() {
  return (
    <p className="mt-2 text-xs text-[var(--color-ink-4)] max-w-3xl">
      Podtyp uvádí jen některé portály (sreality a iDNES ano, ceskereality,
      realitymix a mmreality ne) — u poloviny domů a 39 % komerčních nabídek chybí.
      Žebříček proto zvýhodňuje makléře inzerující na portálech s podtypem; přepínač
      „bez podtypu“ zahrne i neoznačené nabídky.
    </p>
  );
}

function Empty({
  placeLabel,
  hasFirmFilter,
  hasValueFilter,
  hasSubtypeFilter,
}: {
  placeLabel: string;
  hasFirmFilter: boolean;
  hasValueFilter: boolean;
  hasSubtypeFilter: boolean;
}) {
  const extras = [
    hasFirmFilter && 'firmu',
    hasValueFilter && 'cenu',
    hasSubtypeFilter && 'podtyp',
  ].filter((x): x is string => Boolean(x));
  const hint = extras.length === 0
    ? 'Zkuste jiný typ nemovitosti nebo nabídku.'
    : `Zkuste jiný typ nemovitosti, nabídku nebo ${extras.join(' a ')}.`;
  return (
    <div className="mt-6 border border-dashed border-[var(--color-rule-strong)] rounded-[var(--radius-md)] p-8 text-center">
      <p className="text-sm text-[var(--color-ink-2)]">
        Žádní makléři pro tento výběr v {placeLabel}.
      </p>
      <p className="mt-1 text-xs text-[var(--color-ink-3)]">{hint}</p>
    </div>
  );
}
