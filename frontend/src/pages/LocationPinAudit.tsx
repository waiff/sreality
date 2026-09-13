/* Audit poloh na mapě — the pin-loss review surface (migration 510).
 *
 * TEMPORARY. It exists for one decision: migration 503 proposes collapsing the
 * Browse map onto the new location store, and this page is the list of pins
 * that would disappear if it did. Delete the page, lib/pinAudit.ts,
 * components/PinAuditMap.tsx and migration 510's objects once the operator has
 * ruled.
 *
 * Every number on the page comes from ONE payload (the summary RPC), so the
 * matrix, the totals and the list can never tell three different stories. The
 * list is a separate keyset read of the same relation under the same filters.
 */

import { Suspense, useMemo, useState } from 'react';

import ErrorBanner from '@/components/ErrorBanner';
import InfiniteSentinel from '@/components/InfiniteSentinel';
import Spinner from '@/components/Spinner';
import { lazyChunk } from '@/lib/lazyChunk';
import { useQuery } from '@tanstack/react-query';

import { categoryMainLabelPlural, categoryMainLabel, categoryTypeLabel } from '@/lib/enums';
import { fmtArea, fmtCount, fmtCzk, fmtDateSlash } from '@/lib/format';
import { listingRowPath } from '@/lib/listingUrl';
import { portalLabel } from '@/lib/portals';
import { useInfiniteList } from '@/lib/useInfiniteList';
import type { SortSpec } from '@/lib/queries';
import type { KeysetCursor } from '@/lib/keyset';
import {
  EMPTY_PIN_AUDIT_FILTERS,
  PIN_AUDIT_MAP_CAP,
  PIN_AUDIT_PAGE_SIZE,
  PIN_AUDIT_QUALITIES,
  fetchPinAuditPage,
  fetchPinAuditPoints,
  fetchPinAuditSummary,
  summaryRowMatches,
  type PinAuditFilters,
  type PinAuditQuality,
  type PinAuditRow,
  type PinAuditSummaryRow,
} from '@/lib/pinAudit';
import { Link } from 'react-router-dom';

const PinAuditMap = lazyChunk(() => import('@/components/PinAuditMap'));

/* ------------------------------------------------------------------ chrome */

const HEAD = 'text-left text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]';
const TH = 'py-1.5 pr-3 font-medium whitespace-nowrap';
const TD = 'py-1.5 pr-3 align-top';
const NUM = 'py-1.5 pr-3 text-right font-mono tabular-nums';
const ROW = 'border-t border-[var(--color-rule-soft)]';

function Card({
  title,
  lede,
  children,
}: {
  title: string;
  lede: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4">
      <h2 className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
        {title}
      </h2>
      <p className="mt-2 text-[0.78rem] leading-relaxed text-[var(--color-ink-2)] max-w-[52rem]">
        {lede}
      </p>
      <div className="mt-3">{children}</div>
    </section>
  );
}

function Chip({
  on,
  onClick,
  children,
}: {
  on: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={on}
      onClick={onClick}
      className={[
        'rounded-[var(--radius-sm)] border px-2.5 py-1 text-[0.78rem] transition-colors',
        on
          ? 'border-[var(--color-copper)] bg-[var(--color-copper-soft)] text-[var(--color-ink)]'
          : 'border-[var(--color-rule)] text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)]',
      ].join(' ')}
    >
      {children}
    </button>
  );
}

/* -------------------------------------------------------------- vocabulary */

/* Plain Czech, and deliberately not a shorter lie. "Bez dat o poloze" would
 * have been wrong: those ads DO have raw evidence in the claim store — what
 * they have no trace of is evidence the saved verdict actually used. */
const QUALITY_LABEL: Record<PinAuditQuality, string> = {
  active_no_claims: 'inzerát běží · nebylo z čeho polohu určit',
  active_unresolved: 'inzerát běží · podklady byly, poloha nevyšla',
  delisted_no_claims: 'inzerát stažen · nebylo z čeho polohu určit',
  delisted_unresolved: 'inzerát stažen · podklady byly, poloha nevyšla',
};

const QUALITY_SHORT: Record<PinAuditQuality, string> = {
  active_no_claims: 'běží · nebylo z čeho',
  active_unresolved: 'běží · nevyšlo',
  delisted_no_claims: 'stažen · nebylo z čeho',
  delisted_unresolved: 'stažen · nevyšlo',
};

const dash = (v: string | null | undefined): string =>
  v == null || v === '' ? '—' : v;

/* ------------------------------------------------------------------- page */

export default function LocationPinAudit() {
  const [filters, setFilters] = useState<PinAuditFilters>(EMPTY_PIN_AUDIT_FILTERS);
  const [sort, setSort] = useState<SortSpec>({
    field: 'last_seen_at',
    direction: 'desc',
  });
  const [selectedId, setSelectedId] = useState<number | null>(null);

  const summary = useQuery({
    queryKey: ['pin-audit', 'summary'],
    queryFn: fetchPinAuditSummary,
    staleTime: 5 * 60_000,
  });

  const rows: PinAuditSummaryRow[] = useMemo(
    () => summary.data ?? [],
    [summary.data],
  );

  /* The axes are read off the data, never hardcoded: a portal that stops
   * appearing must vanish from the matrix rather than show a stale zero. */
  const sources = useMemo(
    () => Array.from(new Set(rows.map((r) => r.source))).sort(),
    [rows],
  );
  const categories = useMemo(
    () => Array.from(new Set(rows.map((r) => r.category_main ?? ''))).sort(),
    [rows],
  );

  const matched = useMemo(
    () => rows.filter((r) => summaryRowMatches(r, filters)),
    [rows, filters],
  );

  const cell = useMemo(() => {
    const m = new Map<string, number>();
    for (const r of matched) {
      const k = `${r.source}|${r.category_main ?? ''}`;
      m.set(k, (m.get(k) ?? 0) + r.n);
    }
    return m;
  }, [matched]);

  const totalMatched = useMemo(
    () => matched.reduce((a, r) => a + r.n, 0),
    [matched],
  );
  const totalAll = useMemo(() => rows.reduce((a, r) => a + r.n, 0), [rows]);

  const refreshedAt = useMemo(
    () => rows.find((r) => r.refreshed_at != null)?.refreshed_at ?? null,
    [rows],
  );

  const filterKey = JSON.stringify(filters);

  const list = useInfiniteList<PinAuditRow>({
    queryKey: ['pin-audit', 'list', filterKey, sort.field, sort.direction],
    queryFn: async (cursor) => {
      const page = await fetchPinAuditPage(
        filters,
        sort,
        cursor as KeysetCursor | null,
      );
      return { rows: page.rows, nextCursor: page.nextCursor ?? undefined };
    },
    pageSize: PIN_AUDIT_PAGE_SIZE,
    getRowId: (r) => r.listing_id,
  });

  const points = useQuery({
    queryKey: ['pin-audit', 'points', filterKey],
    queryFn: () => fetchPinAuditPoints(filters),
    staleTime: 5 * 60_000,
  });

  const toggle = <T extends string>(list: ReadonlyArray<T>, v: T): T[] =>
    list.includes(v) ? list.filter((x) => x !== v) : [...list, v];

  const err = summary.error ?? list.error ?? points.error;

  return (
    <div className="px-6 pt-5 pb-10 max-w-screen-xl mx-auto">
      <h1 className="text-2xl leading-tight">Audit poloh na mapě</h1>
      <p className="mt-2 text-[0.85rem] leading-relaxed text-[var(--color-ink-2)] max-w-[52rem]">
        Které inzeráty by na mapě přišly o špendlík. Mapa je dnes kreslí ze
        starých souřadnic, ale nový systém pro určování polohy pro ně žádnou
        českou polohu nemá — buď neměl z čeho vyjít, nebo podklady měl a polohu
        z nich nedokázal určit. Kdyby se mapa přepnula jen na nový systém,
        zmizely by právě tyto body. Seznam je tu proto, abyste se na ně mohl
        podívat, než o tom rozhodnete.
      </p>
      <p className="mt-2 text-[0.78rem] text-[var(--color-ink-3)]">
        Celkem {fmtCount(totalAll)} inzerátů
        {refreshedAt ? ` · stav k ${fmtDateSlash(refreshedAt)}` : ''}
        {' · '}seznam se obnovuje každou hodinu
      </p>

      {err ? <ErrorBanner message={(err as Error).message} /> : null}

      <div className="mt-6 grid gap-5">
        <Card
          title="Přehled"
          lede="Řádky jsou portály, sloupce druhy nemovitosti. Číslo v buňce je počet inzerátů, které by přišly o špendlík — po započtení filtrů níže. Kliknutím na buňku se filtr nastaví právě na ni."
        >
          {summary.isLoading ? (
            <Spinner />
          ) : sources.length === 0 ? (
            <p className="text-sm text-[var(--color-ink-3)]">Žádná data.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="text-[0.8rem] min-w-full">
                <thead className={HEAD}>
                  <tr>
                    <th className={TH}>Portál</th>
                    {categories.map((c) => (
                      <th key={c} className={`${TH} text-right`}>
                        {categoryMainLabelPlural(c || null)}
                      </th>
                    ))}
                    <th className={`${TH} text-right`}>Celkem</th>
                  </tr>
                </thead>
                <tbody>
                  {sources.map((s) => {
                    const rowTotal = categories.reduce(
                      (a, c) => a + (cell.get(`${s}|${c}`) ?? 0),
                      0,
                    );
                    return (
                      <tr key={s} className={ROW} data-testid={`pin-audit-matrix-${s}`}>
                        <td className={TD}>{portalLabel(s) ?? s}</td>
                        {categories.map((c) => {
                          const n = cell.get(`${s}|${c}`) ?? 0;
                          return (
                            <td key={c} className={NUM}>
                              {n === 0 ? (
                                <span className="text-[var(--color-ink-4)]">—</span>
                              ) : (
                                <button
                                  type="button"
                                  className="underline decoration-dotted underline-offset-2 hover:text-[var(--color-copper)]"
                                  onClick={() =>
                                    setFilters((f) => ({
                                      ...f,
                                      sources: [s],
                                      categories: [c],
                                    }))
                                  }
                                >
                                  {fmtCount(n)}
                                </button>
                              )}
                            </td>
                          );
                        })}
                        <td className={`${NUM} font-medium`}>{fmtCount(rowTotal)}</td>
                      </tr>
                    );
                  })}
                  <tr className="border-t border-[var(--color-rule-strong)]" data-testid="pin-audit-matrix-total">
                    <td className={`${TD} font-medium`}>Celkem</td>
                    {categories.map((c) => (
                      <td key={c} className={`${NUM} font-medium`}>
                        {fmtCount(
                          sources.reduce(
                            (a, s) => a + (cell.get(`${s}|${c}`) ?? 0),
                            0,
                          ),
                        )}
                      </td>
                    ))}
                    <td className={`${NUM} font-medium`}>{fmtCount(totalMatched)}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <Card
          title="Filtry"
          lede="Filtry platí pro přehled, mapu i seznam zároveň. Nevybrat nic znamená „bez omezení“."
        >
          <div className="grid gap-3">
            <div>
              <p className="text-[0.7rem] uppercase tracking-[0.1em] text-[var(--color-ink-3)]">
                Portál
              </p>
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {sources.map((s) => (
                  <Chip
                    key={s}
                    on={filters.sources.includes(s)}
                    onClick={() =>
                      setFilters((f) => ({ ...f, sources: toggle(f.sources, s) }))
                    }
                  >
                    {portalLabel(s) ?? s}
                  </Chip>
                ))}
              </div>
            </div>
            <div>
              <p className="text-[0.7rem] uppercase tracking-[0.1em] text-[var(--color-ink-3)]">
                Druh nemovitosti
              </p>
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {categories.map((c) => (
                  <Chip
                    key={c}
                    on={filters.categories.includes(c)}
                    onClick={() =>
                      setFilters((f) => ({
                        ...f,
                        categories: toggle(f.categories, c),
                      }))
                    }
                  >
                    {categoryMainLabelPlural(c || null)}
                  </Chip>
                ))}
              </div>
            </div>
            <div>
              <p className="text-[0.7rem] uppercase tracking-[0.1em] text-[var(--color-ink-3)]">
                Proč poloha chybí
              </p>
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {PIN_AUDIT_QUALITIES.map((q) => (
                  <Chip
                    key={q}
                    on={filters.qualities.includes(q)}
                    onClick={() =>
                      setFilters((f) => ({
                        ...f,
                        qualities: toggle(f.qualities, q),
                      }))
                    }
                  >
                    {QUALITY_LABEL[q]}
                  </Chip>
                ))}
              </div>
            </div>
            <div>
              <p className="text-[0.7rem] uppercase tracking-[0.1em] text-[var(--color-ink-3)]">
                Stav inzerátu
              </p>
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {(['all', 'active', 'delisted'] as const).map((s) => (
                  <Chip
                    key={s}
                    on={filters.status === s}
                    onClick={() => setFilters((f) => ({ ...f, status: s }))}
                  >
                    {s === 'all' ? 'Vše' : s === 'active' ? 'Běží' : 'Stažené'}
                  </Chip>
                ))}
              </div>
            </div>
            <div>
              <button
                type="button"
                className="self-start text-[0.78rem] text-[var(--color-ink-3)] underline decoration-dotted underline-offset-2 hover:text-[var(--color-ink)]"
                onClick={() => setFilters(EMPTY_PIN_AUDIT_FILTERS)}
              >
                Zrušit všechny filtry
              </button>
            </div>
          </div>
        </Card>

        <Card
          title="Mapa"
          lede="Staré souřadnice vybraných inzerátů — přesně ty body, o které by mapa přišla. Kliknutím na bod se níže v seznamu zvýrazní jeho řádek."
        >
          {points.isLoading ? (
            <Spinner />
          ) : (points.data?.points.length ?? 0) === 0 ? (
            <p className="text-sm text-[var(--color-ink-3)]">
              Pro zvolené filtry nejsou žádné body.
            </p>
          ) : (
            <>
              <Suspense
                fallback={
                  <div className="h-[24rem] rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)]" />
                }
              >
                <PinAuditMap
                  points={points.data?.points ?? []}
                  selectedId={selectedId}
                  onPick={setSelectedId}
                />
              </Suspense>
              <p className="mt-2 text-[0.75rem] text-[var(--color-ink-3)]">
                {points.data?.capped
                  ? `Zobrazeno prvních ${fmtCount(PIN_AUDIT_MAP_CAP)} bodů z ${fmtCount(totalMatched)} — zúžením filtrů uvidíte zbytek.`
                  : `Zobrazeno ${fmtCount(points.data?.points.length ?? 0)} bodů.`}
              </p>
            </>
          )}
        </Card>

        <Card
          title="Seznam"
          lede="Jeden řádek = jeden inzerát. Název vede na jeho stránku u nás, odkaz „portál“ na původní inzerát. „Verdikt“ shrnuje, co o poloze ví nový systém."
        >
          <div className="mb-2 flex items-center gap-3 text-[0.78rem] text-[var(--color-ink-3)]">
            <span>Řadit podle:</span>
            {(
              [
                { field: 'last_seen_at' as const, label: 'naposledy viděno' },
                { field: 'first_seen_at' as const, label: 'poprvé viděno' },
              ]
            ).map((o) => (
              <Chip
                key={o.field}
                on={sort.field === o.field}
                onClick={() =>
                  setSort((s) =>
                    s.field === o.field
                      ? { ...s, direction: s.direction === 'desc' ? 'asc' : 'desc' }
                      : { field: o.field, direction: 'desc' },
                  )
                }
              >
                {o.label}
                {sort.field === o.field
                  ? sort.direction === 'desc'
                    ? ' ↓'
                    : ' ↑'
                  : ''}
              </Chip>
            ))}
          </div>

          <div className="overflow-x-auto">
            <table className="text-[0.8rem] min-w-full">
              <thead className={HEAD}>
                <tr>
                  <th className={TH}>Portál</th>
                  <th className={TH}>Druh</th>
                  <th className={TH}>Inzerát</th>
                  <th className={`${TH} text-right`}>Cena</th>
                  <th className={TH}>Stav</th>
                  <th className={`${TH} text-right`}>Naposledy viděno</th>
                  <th className={TH}>Proč poloha chybí</th>
                  <th className={TH}>Verdikt</th>
                  <th className={`${TH} text-right`}>Staré souřadnice</th>
                </tr>
              </thead>
              <tbody>
                {list.rows.map((r) => {
                  const href = listingRowPath({
                    source: r.source,
                    source_id_native: r.source_id_native,
                    sreality_id: r.sreality_id,
                    property_id: r.property_id,
                  });
                  const name = [
                    categoryMainLabel(r.category_main),
                    r.disposition,
                    r.area_m2 != null ? fmtArea(r.area_m2) : null,
                  ]
                    .filter(Boolean)
                    .join(' · ');
                  return (
                    <tr
                      key={r.listing_id}
                      className={[
                        ROW,
                        selectedId === r.listing_id
                          ? 'bg-[var(--color-copper-soft)]'
                          : '',
                      ].join(' ')}
                      data-testid={`pin-audit-row-${r.listing_id}`}
                    >
                      <td className={TD}>{portalLabel(r.source) ?? r.source}</td>
                      <td className={TD}>
                        {categoryTypeLabel(r.category_type)}
                      </td>
                      <td className={TD}>
                        <Link
                          to={href}
                          className="text-[var(--color-copper)] hover:underline"
                        >
                          {name || 'Nemovitost'}
                        </Link>
                        <span className="block text-[var(--color-ink-3)]">
                          {dash(r.locality ?? r.district)}
                        </span>
                        {r.source_url ? (
                          <a
                            href={r.source_url}
                            target="_blank"
                            rel="noreferrer"
                            className="text-[0.72rem] text-[var(--color-ink-3)] underline decoration-dotted underline-offset-2"
                          >
                            portál ↗
                          </a>
                        ) : null}
                      </td>
                      <td className={NUM}>{fmtCzk(r.price_czk)}</td>
                      <td className={TD}>{r.is_active ? 'běží' : 'stažen'}</td>
                      <td className={NUM}>{fmtDateSlash(r.last_seen_at)}</td>
                      <td className={TD}>{QUALITY_SHORT[r.quality]}</td>
                      <td className={`${TD} text-[var(--color-ink-3)]`}>
                        {r.has_row
                          ? `${dash(r.country_status)} · ${dash(r.granularity)} · ${dash(r.match_confidence)}`
                          : 'nový systém tento inzerát vůbec nezpracoval'}
                        {r.claims_now && !r.has_claims ? (
                          <span className="block">
                            podklady o poloze přibyly až po vyhodnocení
                          </span>
                        ) : null}
                      </td>
                      <td className={NUM}>
                        {r.legacy_lat.toFixed(5)}, {r.legacy_lng.toFixed(5)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {list.isLoading ? <Spinner /> : null}
          <InfiniteSentinel
            onReach={list.fetchNextPage}
            hasNextPage={list.hasNextPage}
            isFetchingNextPage={list.isFetchingNextPage}
            loadedCount={list.loadedCount}
            total={totalMatched}
            loadingLabel="Načítám další…"
          />
        </Card>
      </div>
    </div>
  );
}
