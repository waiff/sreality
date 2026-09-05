import { useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { ROUTES } from '@/lib/routes';
import { useQuery } from '@tanstack/react-query';
import {
  fetchBrokerDossier,
  fetchBrokerListings,
  type BrokerListing,
  type BrokerRegionShare,
  type BrokerMembership,
} from '../lib/brokers';
import { fmtCount, fmtCzk, fmtArea, fmtRelative } from '../lib/format';
import { portalShort } from '../lib/portals';
import { PickButton } from '../components/controls';
import BrokerContactCard from '@/components/BrokerContactCard';
import { useExploreBrokerModal } from '@/components/ExploreBrokerModal';
import { listingRowPath } from '@/lib/listingUrl';
import { categoryMainLabel, categoryTypeLabel, listingKindLabel } from '@/lib/enums';
import { usePageTitle } from '@/lib/pageTitle';


// Mirror the Žebříček (leaderboard) filter chips, but the broker's own inventory
// defaults to Vše/Vše so nothing is hidden on load.
const CATEGORY_OPTIONS: ReadonlyArray<{ value: string | null; label: string }> = [
  { value: null, label: 'Vše' },
  { value: 'byt', label: 'Byty' },
  { value: 'dum', label: 'Domy' },
  { value: 'pozemek', label: 'Pozemky' },
  { value: 'komercni', label: 'Komerční' },
];
const OFFER_OPTIONS: ReadonlyArray<{ value: string | null; label: string }> = [
  { value: null, label: 'Vše' },
  { value: 'prodej', label: 'Prodej' },
  { value: 'pronajem', label: 'Pronájem' },
];

export default function BrokerDetail() {
  const { id } = useParams<{ id: string }>();
  const brokerId = Number(id);

  // Lifted out of Inventory so the "Explore this broker's listings" modal
  // seeds from the SAME Typ/Nabídka selection the table is filtered to —
  // one cohort definition for both, not two that happen to agree.
  const [categoryMain, setCategoryMain] = useState<string | null>(null);
  const [categoryType, setCategoryType] = useState<string | null>(null);
  const { open: openExploreBroker } = useExploreBrokerModal();

  // One call: identity + firms + regional footprint (+ contacts for an admin).
  // Region names arrive joined, so the old geo-options query and the
  // regionNames-dependent `enabled` gate on the shares query are both gone.
  const dossierQ = useQuery({
    queryKey: ['broker-dossier', brokerId],
    queryFn: () => fetchBrokerDossier(brokerId),
    enabled: Number.isFinite(brokerId),
  });
  const listingsQ = useQuery({
    queryKey: ['broker-listings', brokerId],
    queryFn: () => fetchBrokerListings(brokerId),
    enabled: Number.isFinite(brokerId),
  });

  const dossier = dossierQ.data;
  const b = dossier?.broker;
  usePageTitle(b?.display_name ?? null);

  return (
    <div className="px-6 py-8 max-w-5xl mx-auto text-[var(--color-ink)]">
      <Link
        to={ROUTES.brokers.build()}
        className="text-xs tracking-[0.12em] uppercase text-[var(--color-ink-3)] hover:text-[var(--color-copper)] transition-colors"
      >
        ← Žebříček makléřů
      </Link>

      {dossierQ.isLoading ? (
        <p className="mt-8 text-sm text-[var(--color-ink-3)]">Načítám…</p>
      ) : dossierQ.isError ? (
        /* A failed read is NOT "no such broker" — the old page rendered both as
           "Makléř nenalezen." and hid every outage behind an empty-state. */
        <p className="mt-8 text-sm text-[var(--color-brick)]">
          Makléře se nepodařilo načíst: {(dossierQ.error as Error).message}
        </p>
      ) : !b || !dossier ? (
        <p className="mt-8 text-sm text-[var(--color-ink-3)]">Makléř nenalezen.</p>
      ) : (
        <>
          <header className="mt-3 flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0">
              <h1 className="text-[1.7rem] leading-tight font-[family-name:var(--font-display)]">
                {b.display_name ?? 'Neznámý makléř'}
              </h1>
              <div className="mt-1.5 flex flex-wrap items-center gap-2 text-sm text-[var(--color-ink-3)]">
                <span>{b.firm_name ?? b.firm_domain ?? 'nezávislý / neznámá kancelář'}</span>
                {b.firm_is_franchise && (
                  <span className="text-[0.6rem] tracking-[0.1em] uppercase px-1.5 py-0.5 rounded-[var(--radius-xs)] border border-[var(--color-rule)] text-[var(--color-ink-4)]">
                    franšíza
                  </span>
                )}
                {b.distinct_source_count > 1 && (
                  <span className="text-[0.6rem] tracking-[0.1em] uppercase px-1.5 py-0.5 rounded-[var(--radius-xs)] bg-[var(--color-copper-soft)] text-[var(--color-copper-2)]">
                    {b.distinct_source_count} portály
                  </span>
                )}
              </div>
              <button
                type="button"
                onClick={() =>
                  openExploreBroker({
                    brokerId,
                    brokerName: b.display_name ?? 'Neznámý makléř',
                    categoryMain,
                    categoryType,
                  })
                }
                className="mt-3 inline-flex items-center gap-1.5 px-3 py-1.5 text-[0.8rem] rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[var(--color-ink-2)] hover:border-[var(--color-copper)] hover:text-[var(--color-copper)] transition-colors"
                title="See this broker's listings on the map — same Typ/Nabídka selection as below"
              >
                <MapPinGlyph />
                <span>Explore this broker's listings</span>
              </button>
            </div>
            <BrokerContactCard broker={b} />
          </header>

          {/* Stats strip */}
          <div className="mt-6 grid grid-cols-2 sm:grid-cols-4 border border-[var(--color-rule)] rounded-[var(--radius-md)] divide-x divide-[var(--color-rule-soft)] bg-[var(--color-paper-2)]">
            <Stat
              label="Nemovitosti"
              value={b.active_property_count}
              total={b.property_count}
              hint="aktivní / celkem"
            />
            <Stat
              label="Inzeráty"
              value={b.active_listing_count}
              total={b.listing_count}
              hint="aktivní / celkem"
            />
            <Stat label="Kanceláře" value={dossier.memberships.length} hint="historicky" />
            <Stat label="Regiony" value={dossier.region_shares.length} hint="kde inzeruje" />
          </div>

          <div className="mt-7 grid gap-7 md:grid-cols-2">
            <Footprint shares={dossier.region_shares} />
            <Firms rows={dossier.memberships} />
          </div>

          <Inventory
            rows={listingsQ.data ?? []}
            loading={listingsQ.isLoading}
            error={listingsQ.isError ? (listingsQ.error as Error) : null}
            total={b.listing_count}
            categoryMain={categoryMain}
            categoryType={categoryType}
            onCategoryMainChange={setCategoryMain}
            onCategoryTypeChange={setCategoryType}
          />
        </>
      )}
    </div>
  );
}


/* The locality cell links to the listing when a destination exists, and renders
 * inert text when one does not. `broker_listings_public` declares BOTH
 * `sreality_id` and `property_id` nullable and carries no `source_id_native`, so
 * a row can genuinely have nowhere to go. This used to pass `property_id ?? 0`,
 * which built `/listing?property=0` — a link that type-checked and 404'd. The
 * Portál column still carries `source_url`, so the operator is never stranded. */
function LocalityCell({ l }: { l: BrokerListing }) {
  const label = l.locality ?? l.district ?? '—';
  const dot = !l.is_active && (
    <span
      className="w-1.5 h-1.5 rounded-full bg-[var(--color-brick)] shrink-0"
      title="neaktivní"
    />
  );
  const path = listingRowPath({
    sreality_id: l.sreality_id,
    property_id: l.property_id,
  });

  if (path == null) {
    return (
      <span
        className="flex items-center gap-2 text-[var(--color-ink-3)]"
        title="Tento inzerát nemá v aplikaci vlastní stránku — otevřete jej přes odkaz na portál."
      >
        {dot}
        <span className="truncate font-[family-name:var(--font-sans)]">{label}</span>
      </span>
    );
  }

  return (
    <Link to={path} className="flex items-center gap-2 hover:text-[var(--color-copper-2)]">
      {dot}
      <span className="truncate font-[family-name:var(--font-sans)]">{label}</span>
    </Link>
  );
}
function Stat({
  label,
  value,
  total,
  hint,
}: {
  label: string;
  value: number;
  total?: number;
  hint?: string;
}) {
  return (
    <div className="px-4 py-3">
      <p className="text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
        {label}
      </p>
      <p className="mt-1 font-[family-name:var(--font-mono)] tabular-nums text-[1.35rem] leading-none text-[var(--color-ink)]">
        {fmtCount(value)}
        {total != null && total > value && (
          <span className="text-[var(--color-ink-4)] text-sm"> / {fmtCount(total)}</span>
        )}
      </p>
      {hint && <p className="mt-1 text-[0.62rem] text-[var(--color-ink-4)]">{hint}</p>}
    </div>
  );
}

function Footprint({ shares }: { shares: BrokerRegionShare[] }) {
  const max = shares.reduce((m, s) => Math.max(m, s.active_property_count), 0) || 1;
  return (
    <section>
      <h2 className="text-xs tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
        Kde inzeruje
      </h2>
      <div className="mt-3 border border-[var(--color-rule)] rounded-[var(--radius-md)] bg-[var(--color-paper-2)] px-4 py-3">
        {shares.length === 0 ? (
          <p className="text-sm text-[var(--color-ink-4)]">Bez aktivního regionu.</p>
        ) : (
          <ul className="space-y-2">
            {shares.slice(0, 8).map((s) => (
              <li key={s.geo_id} className="flex items-center gap-3">
                <span className="w-40 shrink-0 truncate text-sm text-[var(--color-ink-2)]">
                  {s.name ?? '—'}
                </span>
                <span className="flex-1 h-1.5 rounded-full bg-[var(--color-inset)] overflow-hidden">
                  <span
                    className="block h-full bg-[var(--color-copper)]"
                    style={{ width: `${Math.max(4, (s.active_property_count / max) * 100)}%` }}
                  />
                </span>
                <span className="w-10 shrink-0 text-right text-xs font-[family-name:var(--font-mono)] tabular-nums text-[var(--color-ink)]">
                  {fmtCount(s.active_property_count)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

function Firms({ rows }: { rows: BrokerMembership[] }) {
  return (
    <section>
      <h2 className="text-xs tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
        Kanceláře
      </h2>
      <div className="mt-3 border border-[var(--color-rule)] rounded-[var(--radius-md)] bg-[var(--color-paper-2)] px-4 py-3">
        {rows.length === 0 ? (
          <p className="text-sm text-[var(--color-ink-4)]">Žádná kancelář (nezávislý).</p>
        ) : (
          <ul className="space-y-2">
            {rows.map((m) => (
              <li key={m.firm_id} className="flex items-center justify-between gap-3">
                <span className="min-w-0 truncate text-sm text-[var(--color-ink-2)]">
                  {m.firm_name ?? m.firm_domain ?? '—'}
                </span>
                <span className="flex items-center gap-2 shrink-0">
                  {m.is_current && (
                    <span className="text-[0.55rem] tracking-[0.1em] uppercase px-1.5 py-0.5 rounded-[var(--radius-xs)] bg-[var(--color-sage-soft)] text-[var(--color-sage)]">
                      aktuální
                    </span>
                  )}
                  <span className="text-xs font-[family-name:var(--font-mono)] tabular-nums text-[var(--color-ink-3)]">
                    {fmtCount(m.listing_count)}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

function Inventory({
  rows,
  loading,
  error,
  total,
  categoryMain,
  categoryType,
  onCategoryMainChange,
  onCategoryTypeChange,
}: {
  rows: BrokerListing[];
  loading: boolean;
  error: Error | null;
  total: number;
  categoryMain: string | null;
  categoryType: string | null;
  onCategoryMainChange: (v: string | null) => void;
  onCategoryTypeChange: (v: string | null) => void;
}) {
  const filtered = useMemo(
    () =>
      rows.filter(
        (l) =>
          (categoryMain === null || l.category_main === categoryMain) &&
          (categoryType === null || l.category_type === categoryType),
      ),
    [rows, categoryMain, categoryType],
  );
  const isFiltered = categoryMain !== null || categoryType !== null;

  return (
    <section className="mt-7">
      <div className="flex items-baseline justify-between">
        <h2 className="text-xs tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
          Inventář
        </h2>
        {rows.length > 0 && (
          <span className="text-[0.7rem] text-[var(--color-ink-4)] tabular-nums">
            {isFiltered
              ? `${fmtCount(filtered.length)} z ${fmtCount(rows.length)}`
              : rows.length < total
                ? `${fmtCount(rows.length)} z ${fmtCount(total)}`
                : fmtCount(total)}
          </span>
        )}
      </div>

      {rows.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-2">
          <InvSegment label="Typ" options={CATEGORY_OPTIONS} value={categoryMain} onChange={onCategoryMainChange} />
          <InvSegment label="Nabídka" options={OFFER_OPTIONS} value={categoryType} onChange={onCategoryTypeChange} />
        </div>
      )}

      <div className="mt-3 overflow-x-auto border border-[var(--color-rule)] rounded-[var(--radius-md)]">
        {loading ? (
          <p className="px-4 py-6 text-sm text-[var(--color-ink-3)]">Načítám inzeráty…</p>
        ) : error ? (
          /* The stats strip above already asserts a listing count from the
             dossier, which now succeeds independently — so a failed inventory
             read rendered as "Žádné inzeráty." would make the page contradict
             itself and read as a delisted broker. */
          <p className="px-4 py-6 text-sm text-[var(--color-brick)]">
            Inzeráty se nepodařilo načíst: {error.message}
          </p>
        ) : rows.length === 0 ? (
          <p className="px-4 py-6 text-sm text-[var(--color-ink-4)]">Žádné inzeráty.</p>
        ) : filtered.length === 0 ? (
          <p className="px-4 py-6 text-sm text-[var(--color-ink-4)]">
            Žádné inzeráty pro zvolený filtr.
          </p>
        ) : (
          <table className="w-full text-sm border-collapse">
            <thead>
              <tr className="border-b border-[var(--color-rule)] bg-[var(--color-paper-2)] text-left">
                {['Lokalita', 'Typ', 'Plocha', 'Cena', 'Portál', 'Viděno'].map((h) => (
                  <th
                    key={h}
                    className="px-3 py-2 font-normal whitespace-nowrap text-[var(--color-ink-3)] text-xs"
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="font-[family-name:var(--font-mono)] text-[0.78rem]">
              {filtered.map((l) => (
                <tr
                  key={l.listing_id}
                  className="border-b border-[var(--color-rule-soft)] last:border-0 hover:bg-[var(--color-paper-2)]"
                >
                  <td className="px-3 py-1.5 max-w-[16rem]">
                    <LocalityCell l={l} />
                  </td>
                  <td className="px-3 py-1.5 whitespace-nowrap text-[var(--color-ink-2)] font-[family-name:var(--font-sans)]">
                    {[
                      // The specific kind (subtype for commercial/houses, else
                      // disposition); the umbrella category word only when no
                      // subtype is shown, to avoid "Ubytování · Komerční" doubling.
                      listingKindLabel(l),
                      l.subtype ? null : categoryMainLabel(l.category_main),
                      l.category_type ? categoryTypeLabel(l.category_type).toLowerCase() : null,
                    ]
                      .filter(Boolean)
                      .join(' · ')}
                  </td>
                  <td className="px-3 py-1.5 whitespace-nowrap tabular-nums text-right text-[var(--color-ink-3)]">
                    {fmtArea(l.area_m2)}
                  </td>
                  <td className="px-3 py-1.5 whitespace-nowrap tabular-nums text-right">
                    {fmtCzk(l.price_czk)}
                  </td>
                  <td className="px-3 py-1.5 whitespace-nowrap text-[var(--color-ink-3)] font-[family-name:var(--font-sans)]">
                    {portalShort(l.source)}
                  </td>
                  <td className="px-3 py-1.5 whitespace-nowrap text-[var(--color-ink-4)] font-[family-name:var(--font-sans)]">
                    {fmtRelative(l.last_seen_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}

function MapPinGlyph() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path
        d="M8 1.5c2.5 0 4.5 2 4.5 4.5 0 3-4.5 8-4.5 8S3.5 9 3.5 6C3.5 3.5 5.5 1.5 8 1.5z"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinejoin="round"
      />
      <circle cx="8" cy="6" r="1.6" stroke="currentColor" strokeWidth="1.2" />
    </svg>
  );
}

function InvSegment({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: ReadonlyArray<{ value: string | null; label: string }>;
  value: string | null;
  onChange: (v: string | null) => void;
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-[0.62rem] tracking-[0.12em] uppercase text-[var(--color-ink-4)]">
        {label}
      </span>
      <div className="flex flex-wrap gap-1">
        {options.map((o) => (
          <PickButton key={String(o.value)} on={o.value === value} onClick={() => onChange(o.value)}>
            {o.label}
          </PickButton>
        ))}
      </div>
    </div>
  );
}
