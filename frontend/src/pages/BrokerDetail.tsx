import { useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import {
  contactState,
  fetchBrokerDossier,
  fetchBrokerListings,
  prettyPhone,
  type BrokerListing,
  type BrokerPublic,
  type BrokerRegionShare,
  type BrokerMembership,
  type ContactState,
} from '../lib/brokers';
import { fmtCount, fmtCzk, fmtArea, fmtRelative } from '../lib/format';
import { portalShort } from '../lib/portals';
import { PickButton } from '../components/controls';
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
        to="/brokers"
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
            </div>
            <ContactCard broker={b} />
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
            total={b.listing_count}
          />
        </>
      )}
    </div>
  );
}

function ContactCard({ broker }: { broker: BrokerPublic }) {
  return (
    <div className="border border-[var(--color-rule)] rounded-[var(--radius-md)] bg-[var(--color-paper-3)] px-4 py-3 min-w-[15rem]">
      <p className="text-[0.6rem] tracking-[0.16em] uppercase text-[var(--color-ink-3)]">
        Kontakt pro oslovení
      </p>
      <div className="mt-2 space-y-1.5">
        <ContactRow
          kind="tel"
          state={contactState(broker.primary_phone, broker.has_phone)}
          format={prettyPhone}
        />
        <ContactRow
          kind="mailto"
          state={contactState(broker.primary_email, broker.has_email)}
          format={(v) => v}
        />
      </div>
    </div>
  );
}

const CONTACT_LABEL: Record<'tel' | 'mailto', string> = {
  tel: 'telefon',
  mailto: 'e-mail',
};

function ContactRow({
  kind,
  state,
  format,
}: {
  kind: 'tel' | 'mailto';
  state: ContactState;
  format: (v: string) => string;
}) {
  const [copied, setCopied] = useState(false);
  if (state.state !== 'value') {
    // "masked" means a contact IS on file but this session may not see it —
    // rendering it as the empty dash would claim the broker has none.
    const masked = state.state === 'masked';
    return (
      <p
        className="text-sm text-[var(--color-ink-4)] font-[family-name:var(--font-mono)]"
        title={masked ? 'Kontakt je viditelný jen pro administrátory.' : undefined}
      >
        {CONTACT_LABEL[kind]} {masked ? '· kontakt na vyžádání' : '—'}
      </p>
    );
  }
  const { value } = state;
  const copy = () => {
    navigator.clipboard?.writeText(value).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    });
  };
  return (
    <div className="flex items-center justify-between gap-3">
      <a
        href={`${kind}:${value}`}
        className="text-sm font-[family-name:var(--font-mono)] text-[var(--color-copper-2)] hover:underline underline-offset-2 truncate"
      >
        {format(value)}
      </a>
      <button
        type="button"
        onClick={copy}
        className="shrink-0 text-[0.6rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)] hover:text-[var(--color-ink)] transition-colors"
      >
        {copied ? 'zkopírováno' : 'kopírovat'}
      </button>
    </div>
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
  total,
}: {
  rows: BrokerListing[];
  loading: boolean;
  total: number;
}) {
  const [categoryMain, setCategoryMain] = useState<string | null>(null);
  const [categoryType, setCategoryType] = useState<string | null>(null);

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
          <InvSegment label="Typ" options={CATEGORY_OPTIONS} value={categoryMain} onChange={setCategoryMain} />
          <InvSegment label="Nabídka" options={OFFER_OPTIONS} value={categoryType} onChange={setCategoryType} />
        </div>
      )}

      <div className="mt-3 overflow-x-auto border border-[var(--color-rule)] rounded-[var(--radius-md)]">
        {loading ? (
          <p className="px-4 py-6 text-sm text-[var(--color-ink-3)]">Načítám inzeráty…</p>
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
                    <Link
                      to={listingRowPath({ sreality_id: l.sreality_id, property_id: l.property_id ?? 0 })}
                      className="flex items-center gap-2 hover:text-[var(--color-copper-2)]"
                    >
                      {!l.is_active && (
                        <span
                          className="w-1.5 h-1.5 rounded-full bg-[var(--color-brick)] shrink-0"
                          title="neaktivní"
                        />
                      )}
                      <span className="truncate font-[family-name:var(--font-sans)]">
                        {l.locality ?? l.district ?? '—'}
                      </span>
                    </Link>
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
