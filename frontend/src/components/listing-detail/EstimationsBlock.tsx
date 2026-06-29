/* The listing page's estimation chapter — the listing is the primary surface
 * for estimations, so everything the old standalone estimation page showed
 * for a linked run renders HERE.
 *
 * Structure: two reference figures side by side — the state's number (MF
 * Cenová mapa reference rent) and ours (the selected run's comparables-based
 * estimate) — then the selected run's full body (yield calculator, re-run,
 * deep-detail popup), then the append-only ledger of every run on any of the
 * property's child listings. ?run=ID selects a run (deep-links from the
 * estimations list and /estimation/:id redirects land here); the latest run
 * is the default. */
import { useEffect, useMemo, useRef } from 'react';
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  estimationKeys,
  fetchEstimationsForListings,
  submitEstimation,
} from '@/lib/queries';
import { ApiError } from '@/lib/api';
import { buildRerunPayload, type RerunInput } from '@/lib/rerun';
import {
  fmtCzk,
  fmtDateSlash,
  fmtAbsolute,
  fmtRelative,
  fmtTime24,
} from '@/lib/format';
import { MfReferenceCard } from '@/components/estimation/MfReferenceCard';
import {
  ConfidencePill,
  RunBody,
  RunStatusChip,
} from '@/components/estimation/RunPanel';
import {
  useNewEstimationModal,
  type NewEstimationPrefill,
} from '@/components/NewEstimationModal';
import type {
  EstimationListResponse,
  EstimationRun,
  ListingPublic,
  MfReferenceRent,
} from '@/lib/types';

export default function EstimationsBlock({
  listing,
  listingIds,
  propertyMf,
  priceDivergence,
  prefill,
}: {
  listing: ListingPublic;
  /* Every child listing of the property (falls back to just this listing
   * until property sources load) — runs are fetched property-grain. */
  listingIds: number[];
  /* The PROPERTY-grain MF (golden record, migration 257). Preferred over the
   * subject advert's per-listing mf_* so every portal's advert of one flat
   * shows the SAME MF; null until the property row loads (then the listing's
   * own value is the fallback). */
  propertyMf?: { mf_reference_rent: MfReferenceRent | null; mf_gross_yield_pct: number | null } | null;
  /* Active siblings advertised at a price != the canonical one the MF/estimate
   * use — surfaced as a note so the operator sees the flat is on the market at
   * more than one price. Null when every active advert agrees. */
  priceDivergence?: {
    usedPrice: number;
    siblings: { source: string; price_czk: number }[];
  } | null;
  prefill?: NewEstimationPrefill;
}) {
  const ids = useMemo(
    () => [...new Set(listingIds)].sort((a, b) => a - b),
    [listingIds],
  );

  const runsQ = useQuery<EstimationListResponse, Error>({
    queryKey: estimationKeys.byListing(ids),
    queryFn: () => fetchEstimationsForListings(ids),
    enabled: ids.length > 0,
    staleTime: 15_000,
    refetchInterval: (q) => {
      const rows = q.state.data?.data ?? [];
      return rows.some(
        (r) => r.status === 'pending' || r.status === 'running',
      )
        ? 3000
        : false;
    },
  });

  const [params] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const runParam = params.get('run');

  const runs = runsQ.data?.data ?? [];
  const selected =
    runs.find((r) => runParam != null && String(r.id) === runParam)
    ?? runs[0]
    ?? null;

  const qc = useQueryClient();
  const rerunMut = useMutation<EstimationRun, ApiError, RerunInput>({
    mutationFn: ({ run, overrides }) =>
      submitEstimation(buildRerunPayload(run, overrides)),
    onSuccess: (run) => {
      qc.invalidateQueries({ queryKey: estimationKeys.all });
      selectRun(run.id);
    },
  });

  const selectRun = (id: number) => {
    const sp = new URLSearchParams(location.search);
    sp.set('run', String(id));
    // navigate (not setSearchParams) so a lingering #feedback hash from a
    // deep-link doesn't re-open the detail popup on every selection.
    navigate(`?${sp.toString()}#estimations`, { replace: true });
  };

  // #estimations deep-links (estimations list, /estimation/:id redirects)
  // scroll the section into view once, after the runs arrive.
  const sectionRef = useRef<HTMLElement | null>(null);
  const scrolledRef = useRef(false);
  const wantsScroll =
    location.hash === '#estimations' || location.hash === '#feedback';
  useEffect(() => {
    if (!wantsScroll || scrolledRef.current) return;
    if (runsQ.isLoading) return;
    scrolledRef.current = true;
    const raf = requestAnimationFrame(() => {
      sectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
    return () => cancelAnimationFrame(raf);
  }, [wantsScroll, runsQ.isLoading]);

  // Prefer the property-grain golden MF; fall back to the subject advert's own
  // value (and finally the selected run's reference_rent for orphan runs). The
  // yield % must track WHICHEVER reference rent we show, so it pairs with the
  // same source.
  const colMfRef = propertyMf?.mf_reference_rent ?? listing.mf_reference_rent ?? null;
  const colMfYield =
    propertyMf?.mf_reference_rent != null
      ? propertyMf.mf_gross_yield_pct
      : listing.mf_reference_rent != null
        ? listing.mf_gross_yield_pct
        : null;
  const mfRef = colMfRef ?? selected?.reference_rent ?? null;

  // Nothing to say: no MF reference, no runs. The section disappears
  // entirely (e.g. land parcels) rather than rendering an empty shell.
  if (!mfRef && runs.length === 0 && !runsQ.isLoading) return null;
  if (runs.length === 0 && runsQ.isLoading && !colMfRef) return null;

  const openedViaFeedbackHash =
    location.hash === '#feedback'
    && selected != null
    && runParam != null
    && String(selected.id) === runParam;

  return (
    <>
      <Hairline />
      <section id="estimations" ref={sectionRef} className="scroll-mt-6">
        <div className="flex items-baseline justify-between">
          <SectionLabel>Estimates</SectionLabel>
          <p className="text-[0.7rem] tracking-wide text-[var(--color-ink-4)] font-mono tabular-nums">
            {runs.length === 0
              ? ''
              : `${runs.length} ${runs.length === 1 ? 'run' : 'runs'}`}
          </p>
        </div>

        {/* Two authorities, side by side: the ministry's reference figure
            and our comparables-based estimate. */}
        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          {mfRef ? (
            <MfReferenceCard
              refRent={mfRef}
              yieldPct={colMfRef != null ? colMfYield : null}
            />
          ) : (
            <EmptyCard label="Odhad nájmu · cenová mapa MF">
              No MF reference for this listing.
            </EmptyCard>
          )}
          {selected ? (
            <RunSummaryCard run={selected} />
          ) : (
            <NoRunsCard prefill={prefill} loading={runsQ.isLoading} />
          )}
        </div>

        {priceDivergence && <PriceDivergenceNote {...priceDivergence} />}

        {selected && (
          <div className="mt-7">
            <RunBody
              key={selected.id}
              run={selected}
              subject={listing}
              embedded
              initialDetailOpen={openedViaFeedbackHash}
              onRerun={(overrides) =>
                rerunMut.mutate({ run: selected, overrides })
              }
              rerunPending={rerunMut.isPending}
              rerunError={rerunMut.error}
            />
          </div>
        )}

        {runs.length > 1 && (
          <RunHistory
            runs={runs}
            selectedId={selected?.id ?? null}
            onPick={selectRun}
          />
        )}
      </section>
    </>
  );
}

/* -------------------------------------------------------------------------- */
/* The "our estimate" card — mirrors the MF card's compact structure          */
/* -------------------------------------------------------------------------- */

function RunSummaryCard({ run }: { run: EstimationRun }) {
  const kind = run.estimate_kind ?? 'rent';
  const isSale = kind === 'sale';
  const value = isSale ? run.estimated_sale_price_czk : run.estimated_monthly_rent_czk;
  const inFlight = run.status === 'pending' || run.status === 'running';

  return (
    <div className="border border-[var(--color-copper)]/30 bg-[var(--color-copper-soft)]/40 rounded-[var(--radius-sm)] p-3">
      <div className="flex items-baseline justify-between gap-3">
        <p className="text-[0.6rem] tracking-[0.16em] uppercase text-[var(--color-ink-4)]">
          Náš odhad · {run.mode === 'agent' ? 'agentní analýza' : 'komparativní'}
        </p>
        {run.status !== 'success' && <RunStatusChip status={run.status} />}
      </div>
      <div className="mt-1 flex items-baseline justify-between gap-3">
        <span className="text-lg font-medium tabular-nums">
          {inFlight ? (
            <span className="text-[var(--color-ink-3)]">Estimating…</span>
          ) : value != null ? (
            <>
              {fmtCzk(value)}
              {!isSale && (
                <span className="ml-1 text-[0.7rem] text-[var(--color-ink-3)]">/měs</span>
              )}
            </>
          ) : (
            <span className="text-[var(--color-ink-3)]">—</span>
          )}
        </span>
        {run.gross_yield_pct != null && (
          <span className="text-[0.72rem] text-[var(--color-ink-3)] tabular-nums">
            hrubý výnos{' '}
            <span className="text-[var(--color-ink)] font-medium">
              {run.gross_yield_pct.toFixed(2)} %
            </span>
          </span>
        )}
      </div>
      {(run.rent_p25_czk != null && run.rent_p75_czk != null && !isSale) && (
        <p className="mt-1.5 text-[0.72rem] text-[var(--color-ink-3)] tabular-nums">
          rozpětí {fmtCzk(run.rent_p25_czk)} – {fmtCzk(run.rent_p75_czk)}
        </p>
      )}
      {(run.sale_p25_czk != null && run.sale_p75_czk != null && isSale) && (
        <p className="mt-1.5 text-[0.72rem] text-[var(--color-ink-3)] tabular-nums">
          rozpětí {fmtCzk(run.sale_p25_czk)} – {fmtCzk(run.sale_p75_czk)}
        </p>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <ConfidencePill confidence={run.confidence} />
      </div>
      <p className="mt-2 text-[0.58rem] text-[var(--color-ink-4)]">
        run #{run.id} · {fmtRelative(run.created_at)}
        {run.skill_name ? ` · ${run.skill_name}` : ''}
      </p>
    </div>
  );
}

function NoRunsCard({
  prefill,
  loading,
}: {
  prefill?: NewEstimationPrefill;
  loading: boolean;
}) {
  const { open } = useNewEstimationModal();
  return (
    <div className="border border-dashed border-[var(--color-rule)] rounded-[var(--radius-sm)] p-3 flex flex-col items-start justify-center gap-2">
      <p className="text-[0.6rem] tracking-[0.16em] uppercase text-[var(--color-ink-4)]">
        Náš odhad
      </p>
      {loading ? (
        <p className="text-sm text-[var(--color-ink-3)]">Loading runs…</p>
      ) : (
        <>
          <p className="text-sm text-[var(--color-ink-3)]">
            No estimation runs for this property yet.
          </p>
          <button
            type="button"
            onClick={() => open(prefill)}
            className="text-sm text-[var(--color-copper)] hover:underline underline-offset-2"
          >
            Run an estimation →
          </button>
        </>
      )}
    </div>
  );
}

function EmptyCard({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="border border-dashed border-[var(--color-rule)] rounded-[var(--radius-sm)] p-3">
      <p className="text-[0.6rem] tracking-[0.16em] uppercase text-[var(--color-ink-4)]">
        {label}
      </p>
      <p className="mt-2 text-sm text-[var(--color-ink-3)]">{children}</p>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Run history — the append-only ledger of every run on this property         */
/* -------------------------------------------------------------------------- */

function RunHistory({
  runs,
  selectedId,
  onPick,
}: {
  runs: EstimationRun[];
  selectedId: number | null;
  onPick: (id: number) => void;
}) {
  return (
    <div className="mt-7">
      <div className="flex items-baseline justify-between">
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
          All runs
        </p>
        <p className="text-[0.7rem] tracking-wide text-[var(--color-ink-4)] font-mono tabular-nums">
          {runs.length}
        </p>
      </div>
      <div className="mt-3 border border-[var(--color-rule)] rounded-[var(--radius-md)] overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)] bg-[var(--color-paper-2)]">
              <th className="px-3 py-2 font-medium">When</th>
              <th className="px-3 py-2 font-medium">Mode</th>
              <th className="px-3 py-2 font-medium text-right">Estimate</th>
              <th className="px-3 py-2 font-medium text-right">Yield</th>
              <th className="px-3 py-2 font-medium">Status</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => {
              const isSelected = r.id === selectedId;
              const isSale = (r.estimate_kind ?? 'rent') === 'sale';
              const value = isSale
                ? r.estimated_sale_price_czk
                : r.estimated_monthly_rent_czk;
              return (
                <tr
                  key={r.id}
                  onClick={() => onPick(r.id)}
                  className={[
                    'cursor-pointer border-t border-[var(--color-rule-soft)] transition-colors',
                    isSelected
                      ? 'bg-[var(--color-copper-soft)]/60'
                      : 'hover:bg-[var(--color-copper-soft)]/30',
                  ].join(' ')}
                >
                  <td
                    className="px-3 py-2 tabular-nums text-[var(--color-ink-2)]"
                    title={fmtAbsolute(r.created_at)}
                  >
                    {fmtDateSlash(r.created_at)}{' '}
                    <span className="text-[0.7rem] text-[var(--color-ink-4)]">
                      {fmtTime24(r.created_at)}
                    </span>
                    {isSelected && (
                      <span className="ml-2 text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-copper)]">
                        viewing
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-[0.8rem] text-[var(--color-ink-3)]">
                    {r.mode}
                    {isSale ? ' · sale' : ''}
                  </td>
                  <td className="px-3 py-2 text-right font-mono tabular-nums text-[var(--color-ink)]">
                    {value != null ? (
                      <>
                        {fmtCzk(value)}
                        {!isSale && (
                          <span className="ml-1 text-[0.7rem] text-[var(--color-ink-3)]">/mo</span>
                        )}
                      </>
                    ) : (
                      <span className="text-[var(--color-ink-4)]">—</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right font-mono tabular-nums text-[var(--color-ink-2)]">
                    {r.gross_yield_pct != null
                      ? `${r.gross_yield_pct.toFixed(2)} %`
                      : '—'}
                  </td>
                  <td className="px-3 py-2">
                    <RunStatusChip status={r.status} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}


/* The same flat is on the market at more than one price: the MF/estimate use the
 * canonical (most-recent active) ask, so we name the active siblings that differ. */
function PriceDivergenceNote({
  usedPrice,
  siblings,
}: {
  usedPrice: number;
  siblings: { source: string; price_czk: number }[];
}) {
  return (
    <p className="mt-3 text-[0.72rem] leading-relaxed text-[var(--color-ink-3)]">
      <span className="font-medium text-[var(--color-ink-2)]">Pozn.:</span>{' '}
      výpočet vychází z ceny{' '}
      <span className="tabular-nums">{fmtCzk(usedPrice)}</span>. Stejná nemovitost
      je aktivně inzerována i za{' '}
      {siblings.map((s, i) => (
        <span key={`${s.source}-${i}`} className="tabular-nums">
          {i > 0 ? ', ' : ''}
          {fmtCzk(s.price_czk)}{' '}
          <span className="text-[var(--color-ink-4)]">({s.source})</span>
        </span>
      ))}
      .
    </p>
  );
}


/* -------------------------------------------------------------------------- */
/* Layout primitives                                                          */
/* -------------------------------------------------------------------------- */

function Hairline() {
  return <div className="my-7 h-px bg-[var(--color-rule)]" />;
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
      {children}
    </p>
  );
}
