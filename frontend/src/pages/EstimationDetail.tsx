import { Suspense, lazy, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery } from '@tanstack/react-query';
import {
  estimationKeys,
  fetchEstimation,
  fetchImagesByListingIds,
  fetchListingById,
  fetchListingsByIds,
  submitEstimation,
} from '@/lib/queries';
import {
  fmtAbsolute,
  fmtArea,
  fmtCzk,
  fmtPricePerM2,
  fmtRelative,
} from '@/lib/format';
import { ApiError, fetchListingSummaries } from '@/lib/api';
import RangeStrip from '@/components/region/RangeStrip';
import Timeline from '@/components/estimation/Timeline';
import { PickButton } from '@/components/controls';
import type {
  ComparableUsed,
  Confidence,
  CreateEstimationIn,
  Disposition,
  EstimationProvider,
  EstimationRun,
  EstimationSource,
  ImagePublic,
  ListingPublic,
  ListingSummaryBatchRow,
  Population,
  SubjectSummary,
  TargetSpecIn,
} from '@/lib/types';

const ComparablesMap = lazy(
  () => import('@/components/estimation/ComparablesMap'),
);
const ComparableModal = lazy(
  () => import('@/components/estimation/ComparableModal'),
);

export default function EstimationDetail() {
  const { id: idParam } = useParams();
  const navigate = useNavigate();
  const id = idParam && /^\d+$/.test(idParam) ? Number(idParam) : null;

  const runQ = useQuery<EstimationRun, Error>({
    queryKey: id != null ? estimationKeys.detail(id) : ['estimations', 'detail', null],
    queryFn: () => fetchEstimation(id as number),
    enabled: id != null,
    staleTime: 60_000,
  });

  const rerunMut = useMutation<EstimationRun, ApiError, RerunInput>({
    mutationFn: ({ run, overrides }) =>
      submitEstimation(buildRerunPayload(run, overrides)),
    onSuccess: (run) => navigate(`/estimation/${run.id}`),
  });

  if (id == null) {
    return <NotFoundState reason="invalid" id={idParam ?? null} />;
  }

  if (runQ.isLoading) {
    return (
      <Page>
        <Crumb />
        <div className="mt-8 text-sm text-[var(--color-ink-3)]">Loading…</div>
      </Page>
    );
  }

  if (runQ.error) {
    const err = runQ.error;
    if (err instanceof ApiError && err.status === 404) {
      return <NotFoundState reason="missing" id={String(id)} />;
    }
    return (
      <Page>
        <Crumb />
        <div className="mt-8 text-sm text-[var(--color-brick)]">
          Failed to load: {err.message}
        </div>
      </Page>
    );
  }

  const run = runQ.data!;
  const isFailed = run.status === 'failed';

  return (
    <Page>
      <Crumb />
      <Header run={run} />

      {!isFailed && (
        <>
          <Hairline />
          <YieldBlock run={run} />
        </>
      )}

      {!isFailed && run.subject_summary && (
        <>
          <Hairline />
          <SubjectSummaryBlock summary={run.subject_summary} />
        </>
      )}

      <Hairline />

      {isFailed ? (
        <FailedBlock run={run} />
      ) : (
        <>
          <RentRange run={run} />
          <Hairline />
        </>
      )}

      {run.warnings && run.warnings.length > 0 && (
        <>
          <Warnings warnings={run.warnings} />
          <Hairline />
        </>
      )}

      <InputRecap run={run} />
      <Hairline />

      {(run.special_instructions || run.contextual_text) && (
        <>
          <OperatorInputsPanel run={run} />
          <Hairline />
        </>
      )}

      <SectionLabel>Trace</SectionLabel>
      <div className="mt-4">
        <Timeline trace={run.trace} runId={run.id} />
      </div>

      {!isFailed && (
        <>
          <Hairline />
          <ComparablesSection run={run} />
        </>
      )}

      <Hairline />
      <RerunBlock
        run={run}
        onRerun={(overrides) => rerunMut.mutate({ run, overrides })}
        pending={rerunMut.isPending}
        error={rerunMut.error}
      />
    </Page>
  );
}

/* -------------------------------------------------------------------------- */
/* Layout primitives                                                          */
/* -------------------------------------------------------------------------- */

function Page({ children }: { children: React.ReactNode }) {
  return <div className="px-6 py-8 max-w-3xl mx-auto">{children}</div>;
}

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

function Crumb() {
  return (
    <Link
      to="/estimations"
      className="inline-flex items-center gap-1.5 text-[0.75rem] tracking-wide text-[var(--color-ink-3)] hover:text-[var(--color-copper)] transition-colors"
    >
      <BackArrow />
      <span>Back to estimations</span>
    </Link>
  );
}

function BackArrow() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
      <polyline
        points="5.5,1.5 1.5,5 5.5,8.5"
        stroke="currentColor"
        strokeWidth="1.25"
        fill="none"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <line
        x1="1.5" y1="5" x2="9" y2="5"
        stroke="currentColor"
        strokeWidth="1.25"
        strokeLinecap="round"
      />
    </svg>
  );
}

/* -------------------------------------------------------------------------- */
/* Header                                                                     */
/* -------------------------------------------------------------------------- */

function Header({ run }: { run: EstimationRun }) {
  const failed = run.status === 'failed';
  const kind = run.estimate_kind ?? 'rent';
  const headline = failed
    ? 'Estimation failed'
    : kind === 'sale'
      ? run.estimated_sale_price_czk != null
        ? fmtCzk(run.estimated_sale_price_czk)
        : 'No estimate produced'
      : run.estimated_monthly_rent_czk != null
        ? `${fmtCzk(run.estimated_monthly_rent_czk)} / mo`
        : 'No estimate produced';

  return (
    <div className="mt-5 flex items-start justify-between gap-6">
      <div className="min-w-0">
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          Estimation · run #{run.id}
        </p>
        <h1
          className="mt-1.5 text-[2.4rem] leading-[1.05] tabular-nums"
          style={{
            fontFamily: 'var(--font-display)',
            fontWeight: 600,
            color: failed ? 'var(--color-brick)' : 'var(--color-ink)',
          }}
        >
          {headline}
        </h1>
        {!failed && run.gross_yield_pct != null && (
          <p className="mt-1.5 text-sm font-mono tabular-nums text-[var(--color-ink-2)]">
            gross yield <span className="text-[var(--color-ink)]">{run.gross_yield_pct.toFixed(2)}&nbsp;%</span>
          </p>
        )}
        <p
          className="mt-2 text-[0.75rem] tracking-wide text-[var(--color-ink-3)]"
          title={fmtAbsolute(run.created_at)}
        >
          {fmtRelative(run.created_at)}
        </p>
      </div>
      <div className="shrink-0 flex flex-col items-end gap-1.5">
        {!failed && <ConfidencePill confidence={run.confidence} />}
        <SourceBadge source={run.source} />
      </div>
    </div>
  );
}

function ConfidencePill({ confidence }: { confidence: Confidence | null }) {
  if (confidence == null) return null;
  const tone = confidenceTone(confidence);
  return (
    <span
      className={[
        'inline-flex items-center gap-1.5 px-2.5 py-1 text-[0.7rem] tracking-wide rounded-[var(--radius-sm)] border',
        tone,
      ].join(' ')}
    >
      <span className="w-1.5 h-1.5 rounded-full bg-current opacity-70" aria-hidden />
      {confidence} confidence
    </span>
  );
}

function confidenceTone(c: Confidence): string {
  if (c === 'high') {
    return 'bg-[var(--color-sage-soft)] text-[var(--color-sage)] border-[var(--color-sage)]/25';
  }
  if (c === 'medium') {
    return 'bg-[var(--color-copper-soft)] text-[var(--color-copper)] border-[var(--color-copper)]/25';
  }
  return 'bg-[var(--color-ochre-soft)] text-[var(--color-ochre)] border-[var(--color-ochre)]/25';
}

function SourceBadge({ source }: { source: EstimationSource }) {
  return (
    <span className="inline-block px-2 py-0.5 text-[0.6rem] tracking-[0.16em] uppercase rounded-[var(--radius-xs)] bg-[var(--color-paper-2)] text-[var(--color-ink-3)] border border-[var(--color-rule)]">
      {source}
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* Rent range                                                                 */
/* -------------------------------------------------------------------------- */

function RentRange({ run }: { run: EstimationRun }) {
  const kind = run.estimate_kind ?? 'rent';
  const isSale = kind === 'sale';
  const median = isSale ? run.estimated_sale_price_czk : run.estimated_monthly_rent_czk;
  const p25 = isSale ? run.sale_p25_czk : run.rent_p25_czk;
  const p75 = isSale ? run.sale_p75_czk : run.rent_p75_czk;
  const sectionLabel = isSale ? 'Sale price range' : 'Rent range';
  const stripLabel = isSale ? 'Sale price (Kč)' : 'Monthly rent (Kč)';
  if (median == null || p25 == null || p75 == null) {
    return (
      <div>
        <SectionLabel>{sectionLabel}</SectionLabel>
        <p className="mt-2 text-sm text-[var(--color-ink-3)]">
          Range data not available.
        </p>
      </div>
    );
  }
  return (
    <div>
      <SectionLabel>{sectionLabel}</SectionLabel>
      <div className="mt-3">
        <RangeStrip
          label={stripLabel}
          triple={{ p25, p50: median, p75 }}
          format={(n) => fmtCzk(n)}
        />
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Yield                                                                      */
/*                                                                            */
/* Pure client-side calculation. Three editable inputs (monthly rent,         */
/* fond-oprav cost per m², listing price) drive the yield % live; the         */
/* listing area comes from the run's input spec. Nothing here is persisted    */
/* — operators tweak to model "what if" scenarios without writing a re-run.   */
/* -------------------------------------------------------------------------- */

const DEFAULT_FOND_CZK_PER_M2 = 10;

function YieldBlock({ run }: { run: EstimationRun }) {
  const kind = run.estimate_kind ?? 'rent';
  const areaM2 = run.input_spec?.area_m2 ?? null;
  const defaultRent = run.estimated_monthly_rent_czk;

  /* When the operator pasted a sreality URL, fetch the subject listing
   * so a sale URL can prefill the listing-price input from the actual
   * asking price. Skipped for non-sreality (no DB row to read) and for
   * spec-only runs. */
  const subjectQ = useQuery<ListingPublic | null, Error>({
    queryKey: ['estimation-subject-listing', run.input_sreality_id],
    queryFn: () => fetchListingById(run.input_sreality_id as number),
    enabled: run.input_sreality_id != null,
    staleTime: 60_000,
  });

  const subjectSalePrice =
    subjectQ.data && subjectQ.data.category_type === 'prodej'
      ? subjectQ.data.price_czk
      : null;

  const defaultPrice =
    subjectSalePrice ??
    run.input_purchase_price_czk ??
    (kind === 'sale' ? run.estimated_sale_price_czk : null);

  const [rent, setRent] = useState<number | null>(defaultRent);
  const [costPerM2, setCostPerM2] = useState<number | null>(DEFAULT_FOND_CZK_PER_M2);
  const [price, setPriceState] = useState<number | null>(defaultPrice);
  const [priceTouched, setPriceTouched] = useState(false);

  /* Sync the price input to the latest default until the operator types
   * into it — handles the listing query resolving after first render. */
  useEffect(() => {
    if (!priceTouched) setPriceState(defaultPrice);
  }, [defaultPrice, priceTouched]);

  const setPrice = (v: number | null) => {
    setPriceTouched(true);
    setPriceState(v);
  };

  const fondOprav =
    costPerM2 != null && areaM2 != null ? costPerM2 * areaM2 : null;

  const yieldPct =
    rent != null && fondOprav != null && price != null && price > 0
      ? ((rent - fondOprav) * 12) / price * 100
      : null;

  const priceHint = subjectSalePrice != null
    ? 'Default: from sale listing'
    : defaultPrice != null
      ? 'Default: from inputs'
      : 'Set purchase price';

  return (
    <div>
      <div className="flex items-baseline justify-between">
        <SectionLabel>Yield</SectionLabel>
        <p className="text-[0.7rem] tracking-wide text-[var(--color-ink-4)]">
          live calculation
        </p>
      </div>

      <div className="mt-4 px-5 py-5 rounded-[var(--radius-md)] border border-[var(--color-copper)]/30 bg-[var(--color-copper-soft)]">
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          Gross yield
        </p>
        <p
          className="mt-1.5 tabular-nums leading-none"
          style={{
            fontFamily: 'var(--font-display)',
            fontWeight: 600,
            fontSize: '3rem',
            color: yieldPct != null ? 'var(--color-copper)' : 'var(--color-ink-4)',
          }}
        >
          {yieldPct != null ? `${yieldPct.toFixed(2)} %` : '—'}
        </p>
        <p className="mt-2 text-[0.78rem] text-[var(--color-ink-3)] font-mono tabular-nums">
          ((rent − fond oprav a SVJ) × 12) ÷ listing price
        </p>
      </div>

      <div className="mt-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <YieldNumField
          label="Monthly rent"
          value={rent}
          step="100"
          suffix="Kč"
          onChange={setRent}
          hint={defaultRent != null ? 'Default: median estimate' : 'No estimate — set manually'}
        />
        <YieldNumField
          label="Fond oprav + SVJ"
          value={costPerM2}
          step="1"
          suffix="Kč/m²"
          onChange={setCostPerM2}
          hint={
            fondOprav != null
              ? `= ${fmtCzk(Math.round(fondOprav))} / mo`
              : areaM2 == null
                ? 'Area unavailable — fond oprav not computed'
                : 'Cost per m² of usable area'
          }
        />
        <YieldNumField
          label="Area"
          value={areaM2}
          step="0.1"
          suffix="m²"
          onChange={() => undefined}
          readOnly
          hint="From listing"
        />
        <YieldNumField
          label="Listing price"
          value={price}
          step="100000"
          suffix="Kč"
          onChange={setPrice}
          hint={priceHint}
        />
      </div>
    </div>
  );
}

function YieldNumField({
  label,
  value,
  step,
  suffix,
  hint,
  readOnly,
  onChange,
}: {
  label: string;
  value: number | null;
  step?: string;
  suffix?: string;
  hint?: string;
  readOnly?: boolean;
  onChange: (v: number | null) => void;
}) {
  return (
    <div>
      <FieldLabel>{label}</FieldLabel>
      <div className="mt-1.5 flex items-stretch gap-2 min-w-0">
        <input
          type="text"
          inputMode="decimal"
          readOnly={readOnly}
          value={value == null ? '' : String(value)}
          step={step}
          onChange={(e) => {
            if (readOnly) return;
            const raw = e.target.value.trim().replace(',', '.');
            if (raw === '') return onChange(null);
            const n = Number(raw);
            if (Number.isFinite(n)) onChange(n);
          }}
          className={[
            'flex-1 min-w-0 px-3 py-2 text-sm font-mono tabular-nums rounded-[var(--radius-sm)] border text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)]',
            readOnly
              ? 'bg-[var(--color-paper-2)] border-[var(--color-rule)] text-[var(--color-ink-2)] cursor-default'
              : 'bg-[var(--color-inset)] border-[var(--color-rule)]',
          ].join(' ')}
        />
        {suffix && (
          <span className="self-center text-[0.78rem] tracking-wide text-[var(--color-ink-3)]">
            {suffix}
          </span>
        )}
      </div>
      {hint && (
        <p className="mt-1.5 text-[0.7rem] text-[var(--color-ink-4)] leading-relaxed">
          {hint}
        </p>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Failed block                                                               */
/* -------------------------------------------------------------------------- */

function FailedBlock({ run }: { run: EstimationRun }) {
  return (
    <div>
      <SectionLabel>Error</SectionLabel>
      <pre className="mt-3 px-3 py-2.5 rounded-[var(--radius-sm)] border border-[var(--color-brick)]/30 bg-[var(--color-brick-soft)] text-[var(--color-brick)] text-[0.8rem] font-mono whitespace-pre-wrap break-words">
        {run.error_message ?? 'Unknown error.'}
      </pre>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Warnings                                                                   */
/* -------------------------------------------------------------------------- */

function Warnings({ warnings }: { warnings: string[] }) {
  return (
    <div>
      <SectionLabel>Warnings</SectionLabel>
      <ul className="mt-3 space-y-2">
        {warnings.map((w, i) => (
          <li
            key={i}
            className="px-3 py-2 rounded-[var(--radius-sm)] border border-[var(--color-ochre)]/30 bg-[var(--color-ochre-soft)] text-[0.85rem] text-[var(--color-ochre)]"
          >
            {w}
          </li>
        ))}
      </ul>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Input recap                                                                */
/* -------------------------------------------------------------------------- */

function InputRecap({ run }: { run: EstimationRun }) {
  const spec = run.input_spec;
  const facts: Array<[string, string | null]> = [];

  if (spec) {
    facts.push(['Coords', spec.lat != null && spec.lng != null
      ? `${spec.lat.toFixed(5)}, ${spec.lng.toFixed(5)}`
      : null]);
    facts.push(['Area', spec.area_m2 != null ? fmtArea(spec.area_m2) : null]);
    facts.push(['Disposition', spec.disposition ?? null]);
    if (spec.floor != null) facts.push(['Floor', String(spec.floor)]);
    if (spec.exclude_ids.length > 0) {
      facts.push(['Excluded', spec.exclude_ids.map(String).join(', ')]);
    }
  }

  if (run.input_purchase_price_czk != null) {
    facts.push(['Purchase price', fmtCzk(run.input_purchase_price_czk)]);
  }

  return (
    <div>
      <SectionLabel>Inputs</SectionLabel>
      {run.input_url && (
        <p className="mt-3 text-sm">
          <span className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)] mr-2">URL</span>
          <a
            href={run.input_url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-[var(--color-copper)] hover:text-[var(--color-copper-2)] underline-offset-2 hover:underline break-all"
          >
            {run.input_url}
          </a>
        </p>
      )}
      {run.input_sreality_id != null && (
        <p className="mt-1.5 text-sm">
          <span className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)] mr-2">Listing</span>
          <Link
            to={`/listing/${run.input_sreality_id}`}
            className="font-mono tabular-nums text-[var(--color-copper)] hover:text-[var(--color-copper-2)] underline-offset-2 hover:underline"
          >
            id {run.input_sreality_id}
          </Link>
        </p>
      )}

      <dl className="mt-4 grid grid-cols-2 md:grid-cols-3 gap-x-6 gap-y-3">
        {facts.map(([label, value]) => (
          <Fact key={label} label={label} value={value} />
        ))}
      </dl>

      {run.parent_run_id != null && (
        <p className="mt-4 text-[0.78rem] text-[var(--color-ink-3)]">
          Re-run of{' '}
          <Link
            to={`/estimation/${run.parent_run_id}`}
            className="font-mono tabular-nums text-[var(--color-copper)] hover:underline underline-offset-2"
          >
            #{run.parent_run_id}
          </Link>
          {run.rerun_reason ? ` · ${run.rerun_reason}` : ''}
        </p>
      )}
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string | null }) {
  return (
    <div>
      <dt className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
        {label}
      </dt>
      <dd
        className={[
          'mt-1 text-sm font-mono tabular-nums',
          value == null ? 'text-[var(--color-ink-4)]' : 'text-[var(--color-ink)]',
        ].join(' ')}
      >
        {value ?? '—'}
      </dd>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Operator inputs panel — read-only display of special_instructions /        */
/* contextual_text on a terminal run. Immutable here; mutation happens by     */
/* re-run from RerunBlock, which carries the inputs forward by default.       */
/* -------------------------------------------------------------------------- */

function OperatorInputsPanel({ run }: { run: EstimationRun }) {
  const hasInstr = !!run.special_instructions;
  const hasCtx = !!run.contextual_text;
  if (!hasInstr && !hasCtx) return null;
  return (
    <div className="mt-6">
      <SectionLabel>Operator context</SectionLabel>
      <div className="mt-3 space-y-3">
        {hasInstr && (
          <div>
            <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
              Special instructions
            </p>
            <pre className="mt-1 whitespace-pre-wrap text-[0.85rem] leading-relaxed font-sans text-[var(--color-ink)]">
              {run.special_instructions}
            </pre>
          </div>
        )}
        {hasCtx && (
          <div>
            <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
              Property context
            </p>
            <pre className="mt-1 whitespace-pre-wrap text-[0.85rem] leading-relaxed font-sans text-[var(--color-ink)]">
              {run.contextual_text}
            </pre>
          </div>
        )}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Subject summary (top of page when sreality_id is set)                      */
/* -------------------------------------------------------------------------- */

function SubjectSummaryBlock({ summary }: { summary: SubjectSummary }) {
  const body = summary.summary;
  const empty =
    !body.location_summary &&
    !body.building_summary &&
    !body.apartment_summary;
  if (empty) return null;
  return (
    <div>
      <SectionLabel>Subject summary</SectionLabel>
      {body.headline && (
        <p className="mt-3 text-[1.05rem] text-[var(--color-ink)] leading-snug">
          {body.headline}
        </p>
      )}
      <div className="mt-4 grid gap-4 md:grid-cols-3">
        <SummaryCell label="Location" text={body.location_summary} />
        <SummaryCell label="Building" text={body.building_summary} />
        <SummaryCell label="Apartment" text={body.apartment_summary} />
      </div>
    </div>
  );
}

function SummaryCell({ label, text }: { label: string; text?: string | null }) {
  return (
    <div>
      <p className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
        {label}
      </p>
      <p className="mt-1.5 text-sm text-[var(--color-ink)] leading-relaxed">
        {text || <span className="text-[var(--color-ink-4)]">—</span>}
      </p>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Comparables — map + table + popup                                          */
/* -------------------------------------------------------------------------- */

function ComparablesSection({ run }: { run: EstimationRun }) {
  const comps = useMemo(
    () => sortedComparables(run.comparables_used ?? []),
    [run.comparables_used],
  );
  const ids = useMemo(() => comps.map((c) => c.sreality_id), [comps]);
  const [activeId, setActiveId] = useState<number | null>(null);

  const listingsQ = useQuery<Map<number, ListingPublic>, Error>({
    queryKey: ['estimation-comparables', 'listings', ids.join(',')],
    queryFn: () => fetchListingsByIds(ids),
    enabled: ids.length > 0,
    staleTime: 60_000,
  });

  const imagesQ = useQuery<Map<number, ImagePublic[]>, Error>({
    queryKey: ['estimation-comparables', 'images', ids.join(',')],
    queryFn: () => fetchImagesByListingIds(ids, 6),
    enabled: ids.length > 0,
    staleTime: 5 * 60_000,
  });

  const summariesQ = useQuery<Map<number, ListingSummaryBatchRow>, Error>({
    queryKey: [
      'estimation-comparables',
      'summaries',
      comps.map((c) => `${c.sreality_id}:${c.snapshot_id ?? ''}`).join(','),
    ],
    queryFn: async () => {
      const items = comps.map((c) => ({
        sreality_id: c.sreality_id,
        snapshot_id: c.snapshot_id,
      }));
      const res = await fetchListingSummaries(items);
      const map = new Map<number, ListingSummaryBatchRow>();
      for (const row of res.data) map.set(row.sreality_id, row);
      return map;
    },
    enabled: ids.length > 0,
    staleTime: 10 * 60_000,
  });

  if (comps.length === 0) {
    return (
      <div>
        <SectionLabel>Comparables</SectionLabel>
        <p className="mt-2 text-sm text-[var(--color-ink-3)]">
          No comparables recorded for this run.
        </p>
      </div>
    );
  }

  const subjectLat = run.input_spec?.lat ?? null;
  const subjectLng = run.input_spec?.lng ?? null;
  const listings = listingsQ.data ?? new Map<number, ListingPublic>();
  const images = imagesQ.data ?? new Map<number, ImagePublic[]>();
  const summaries = summariesQ.data ?? new Map<number, ListingSummaryBatchRow>();

  const mapPoints = comps
    .map((c) => {
      const l = listings.get(c.sreality_id);
      if (!l || l.lat == null || l.lng == null) return null;
      return {
        sreality_id: l.sreality_id,
        lat: l.lat,
        lng: l.lng,
        price_czk: l.price_czk,
        area_m2: l.area_m2,
        disposition: l.disposition,
        district: l.district,
      };
    })
    .filter((p): p is NonNullable<typeof p> => p !== null);

  const activeListing = activeId != null ? listings.get(activeId) ?? null : null;
  const activeSummary = activeId != null ? summaries.get(activeId) ?? null : null;
  const activeImages = activeId != null ? images.get(activeId) ?? [] : [];

  return (
    <div>
      <div className="flex items-baseline justify-between">
        <SectionLabel>Comparables</SectionLabel>
        <p className="text-[0.7rem] tracking-wide text-[var(--color-ink-4)] font-mono tabular-nums">
          {comps.length}
        </p>
      </div>

      {subjectLat != null && subjectLng != null && mapPoints.length > 0 && (
        <div className="mt-4">
          <Suspense
            fallback={
              <div className="h-80 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)]" />
            }
          >
            <ComparablesMap
              subject={{ lat: subjectLat, lng: subjectLng }}
              comparables={mapPoints}
              onPick={(id) => setActiveId(id)}
            />
          </Suspense>
        </div>
      )}

      <div className="mt-4 rounded-[var(--radius-md)] border border-[var(--color-rule)] overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-[var(--color-paper-2)] border-b border-[var(--color-rule)]">
            <tr>
              <Th align="left">ID</Th>
              <Th align="right">Price</Th>
              <Th align="right">Area</Th>
              <Th align="left">Disp.</Th>
              <Th align="left">Summary</Th>
              <Th align="right">Age</Th>
            </tr>
          </thead>
          <tbody>
            {comps.map((c) => (
              <ComparableRow
                key={`${c.sreality_id}:${c.snapshot_id ?? 'none'}`}
                comp={c}
                listing={listings.get(c.sreality_id) ?? null}
                summary={summaries.get(c.sreality_id) ?? null}
                onOpen={() => setActiveId(c.sreality_id)}
                listingsLoading={listingsQ.isLoading}
                summariesLoading={summariesQ.isLoading}
              />
            ))}
          </tbody>
        </table>
      </div>

      {activeId != null && activeListing && (
        <Suspense fallback={null}>
          <ComparableModal
            listing={activeListing}
            images={activeImages}
            summary={activeSummary?.summary ?? null}
            summaryError={activeSummary?.error ?? null}
            summaryLoading={summariesQ.isLoading}
            onClose={() => setActiveId(null)}
          />
        </Suspense>
      )}
    </div>
  );
}

function ComparableRow({
  comp,
  listing,
  summary,
  onOpen,
  listingsLoading,
  summariesLoading,
}: {
  comp: ComparableUsed;
  listing: ListingPublic | null;
  summary: ListingSummaryBatchRow | null;
  onOpen: () => void;
  listingsLoading: boolean;
  summariesLoading: boolean;
}) {
  const ppm = listing
    ? fmtPricePerM2(listing.price_czk, listing.area_m2)
    : null;
  const locText =
    summary?.summary?.location_summary ??
    (summariesLoading
      ? 'Generating…'
      : summary?.error
        ? `Summary unavailable (${summary.error})`
        : '—');
  return (
    <tr
      onClick={onOpen}
      className="cursor-pointer border-b border-[var(--color-rule-soft)] last:border-b-0 hover:bg-[var(--color-copper-soft)]/40 transition-colors"
    >
      <td className="px-3 py-2 align-middle">
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onOpen();
          }}
          className="font-mono tabular-nums text-[var(--color-copper)] hover:underline underline-offset-2"
        >
          {comp.sreality_id}
        </button>
      </td>
      <td className="px-3 py-2 align-middle text-right font-mono tabular-nums text-[var(--color-ink)]">
        {listingsLoading && !listing
          ? <span className="text-[var(--color-ink-4)]">…</span>
          : listing ? fmtCzk(listing.price_czk) : '—'}
        {ppm && ppm !== '—' && (
          <span className="block text-[0.7rem] text-[var(--color-ink-4)] font-normal">
            {ppm}
          </span>
        )}
      </td>
      <td className="px-3 py-2 align-middle text-right font-mono tabular-nums text-[var(--color-ink-2)]">
        {listing ? fmtArea(listing.area_m2) : '—'}
      </td>
      <td className="px-3 py-2 align-middle font-mono tabular-nums text-[var(--color-ink-2)]">
        {listing?.disposition ?? '—'}
      </td>
      <td className="px-3 py-2 align-middle text-[0.82rem] text-[var(--color-ink-2)]">
        <span className="line-clamp-2 leading-snug">{locText}</span>
      </td>
      <td className="px-3 py-2 align-middle text-right font-mono tabular-nums text-[var(--color-ink-3)] text-[0.78rem]">
        {comp.data_age_days != null ? `${comp.data_age_days} d` : '—'}
        {comp.verified_during_estimate && (
          <span className="block text-[var(--color-sage)]">verified</span>
        )}
      </td>
    </tr>
  );
}

function sortedComparables(comps: ComparableUsed[]): ComparableUsed[] {
  return [...comps].sort(
    (a, b) => (a.data_age_days ?? Infinity) - (b.data_age_days ?? Infinity),
  );
}

function Th({ align, children }: { align: 'left' | 'right'; children: React.ReactNode }) {
  return (
    <th
      scope="col"
      className={[
        'px-3 py-2 text-[0.65rem] tracking-[0.14em] uppercase font-medium text-[var(--color-ink-3)]',
        align === 'right' ? 'text-right' : 'text-left',
      ].join(' ')}
    >
      {children}
    </th>
  );
}

/* -------------------------------------------------------------------------- */
/* Re-run                                                                     */
/* -------------------------------------------------------------------------- */

/* The "Adjust & re-run" panel — collapsed by default. The fast path
 * (re-run unchanged) lives on the same row as the expander; expanding
 * reveals the editable spec form pre-filled from the run. Only the
 * fields a sane re-run actually flips are exposed; building/amenity
 * attributes belong to the listing scrape, not the run, and aren't
 * something the operator can override here. */

type RerunOverrides = {
  spec?: TargetSpecIn;
  estimate_kind?: 'rent' | 'sale';
  provider?: EstimationProvider;
  population?: Population;
  purchase_price_czk?: number | null;
  expected_monthly_rent_czk?: number | null;
};

interface RerunInput {
  run: EstimationRun;
  overrides?: RerunOverrides;
}

const DISPOSITIONS: ReadonlyArray<Disposition> = [
  '1+kk', '1+1',
  '2+kk', '2+1',
  '3+kk', '3+1',
  '4+kk', '4+1',
  '5+kk', '5+1',
];

interface AdjustState {
  lat: number | null;
  lng: number | null;
  area_m2: number | null;
  disposition: Disposition | null;
  floor: number | null;
  estimate_kind: 'rent' | 'sale';
  provider: EstimationProvider;
  population: Population;
  purchase_price_czk: number | null;
  expected_monthly_rent_czk: number | null;
}

function adjustStateFromRun(run: EstimationRun): AdjustState {
  const spec = run.input_spec;
  return {
    lat: spec?.lat ?? null,
    lng: spec?.lng ?? null,
    area_m2: spec?.area_m2 ?? null,
    disposition: spec?.disposition ?? null,
    floor: spec?.floor ?? null,
    estimate_kind: run.estimate_kind ?? 'rent',
    provider: 'anthropic',
    population: 'active',
    purchase_price_czk: run.input_purchase_price_czk,
    expected_monthly_rent_czk: null,
  };
}

function RerunBlock({
  run,
  onRerun,
  pending,
  error,
}: {
  run: EstimationRun;
  onRerun: (overrides?: RerunOverrides) => void;
  pending: boolean;
  error: ApiError | null;
}) {
  const canRerun = run.input_url != null || run.input_spec != null;
  const [expanded, setExpanded] = useState(false);
  const [state, setState] = useState<AdjustState>(() => adjustStateFromRun(run));

  const valid =
    state.lat != null && Number.isFinite(state.lat) &&
    state.lng != null && Number.isFinite(state.lng) &&
    state.area_m2 != null && state.area_m2 > 0 &&
    state.disposition != null;

  const submitWithEdits = () => {
    const overrides: RerunOverrides = {
      spec: {
        lat: state.lat as number,
        lng: state.lng as number,
        area_m2: state.area_m2,
        disposition: state.disposition,
        floor: state.floor,
        exclude_ids: run.input_spec?.exclude_ids ?? [],
      },
      estimate_kind: state.estimate_kind,
      provider: state.provider,
      population: state.population,
      purchase_price_czk:
        state.estimate_kind === 'rent' ? state.purchase_price_czk : null,
      expected_monthly_rent_czk:
        state.estimate_kind === 'sale' ? state.expected_monthly_rent_czk : null,
    };
    onRerun(overrides);
  };

  return (
    <div>
      <SectionLabel>Re-run</SectionLabel>
      <p className="mt-2 text-[0.78rem] text-[var(--color-ink-3)] leading-relaxed">
        Re-runs link back via parent_run_id. The original record is immutable.
        Adjust to fix a wrong scrape or try different agent settings.
      </p>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={!canRerun || pending}
          onClick={() => onRerun()}
          className={[
            'px-4 py-2 text-sm rounded-[var(--radius-sm)] border transition-colors',
            !canRerun || pending
              ? 'bg-[var(--color-rule-strong)] text-[var(--color-ink-4)] border-[var(--color-rule-strong)] cursor-not-allowed'
              : 'bg-[var(--color-paper-2)] text-[var(--color-ink-2)] border-[var(--color-rule)] hover:border-[var(--color-copper)] hover:text-[var(--color-copper)]',
          ].join(' ')}
        >
          {pending ? 'Re-running…' : 'Re-run with same inputs'}
        </button>

        {canRerun && run.input_spec && (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            aria-expanded={expanded}
            className="inline-flex items-center gap-1.5 px-3 py-2 text-sm rounded-[var(--radius-sm)] text-[var(--color-ink-3)] hover:text-[var(--color-copper)] transition-colors"
          >
            <span>Adjust inputs</span>
            <Chevron open={expanded} />
          </button>
        )}

        {!canRerun && (
          <span className="text-[0.78rem] text-[var(--color-ink-3)]">
            Original inputs unavailable.
          </span>
        )}
      </div>

      {expanded && run.input_spec && (
        <AdjustPanel
          state={state}
          onChange={setState}
          valid={valid}
          pending={pending}
          onSubmit={submitWithEdits}
        />
      )}

      {error && (
        <p className="mt-2 text-[0.78rem] text-[var(--color-brick)]">
          {error.message || `Re-run failed (HTTP ${error.status}).`}
        </p>
      )}
    </div>
  );
}

function AdjustPanel({
  state,
  onChange,
  valid,
  pending,
  onSubmit,
}: {
  state: AdjustState;
  onChange: (next: AdjustState) => void;
  valid: boolean;
  pending: boolean;
  onSubmit: () => void;
}) {
  const set = <K extends keyof AdjustState>(key: K, value: AdjustState[K]) =>
    onChange({ ...state, [key]: value });

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (valid && !pending) onSubmit();
      }}
      className="mt-4 px-4 py-4 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] space-y-5"
    >
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <NumField
          label="Latitude"
          required
          value={state.lat}
          step="0.000001"
          placeholder="50.0875"
          onChange={(v) => set('lat', v)}
        />
        <NumField
          label="Longitude"
          required
          value={state.lng}
          step="0.000001"
          placeholder="14.4205"
          onChange={(v) => set('lng', v)}
        />
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <NumField
          label="Area"
          required
          value={state.area_m2}
          step="0.1"
          placeholder="50"
          suffix="m²"
          onChange={(v) => set('area_m2', v)}
        />
        <NumField
          label="Floor"
          value={state.floor}
          step="1"
          placeholder="—"
          onChange={(v) => set('floor', v != null ? Math.round(v) : null)}
        />
      </div>

      <div>
        <FieldLabel required>Disposition</FieldLabel>
        <div className="mt-1.5 grid grid-cols-5 gap-1.5">
          {DISPOSITIONS.map((d) => (
            <PickButton
              key={d}
              on={state.disposition === d}
              onClick={() =>
                set('disposition', state.disposition === d ? null : d)
              }
              variant="solid"
              className="font-mono tabular-nums"
            >
              {d}
            </PickButton>
          ))}
        </div>
      </div>

      <div>
        <FieldLabel>Estimate kind</FieldLabel>
        <SegRow
          options={[
            { value: 'rent', label: 'Rent (monthly)' },
            { value: 'sale', label: 'Sale price' },
          ]}
          value={state.estimate_kind}
          onChange={(v) => set('estimate_kind', v)}
        />
      </div>

      {state.estimate_kind === 'rent' && (
        <>
          <div>
            <FieldLabel>Model provider</FieldLabel>
            <SegRow
              options={[
                { value: 'anthropic', label: 'Claude' },
                { value: 'gemini', label: 'Gemini' },
              ]}
              value={state.provider}
              onChange={(v) => set('provider', v)}
            />
          </div>
          <div>
            <FieldLabel>Comparable population</FieldLabel>
            <SegRow
              options={[
                { value: 'active', label: 'Active' },
                { value: 'delisted', label: 'Delisted' },
                { value: 'all', label: 'Both' },
              ]}
              value={state.population}
              onChange={(v) => set('population', v)}
            />
          </div>
        </>
      )}

      {state.estimate_kind === 'rent' ? (
        <NumField
          label="Purchase price"
          value={state.purchase_price_czk}
          step="100000"
          placeholder="—"
          suffix="Kč"
          onChange={(v) =>
            set('purchase_price_czk', v != null ? Math.round(v) : null)
          }
          hint="Optional. Adds gross yield % to the result."
        />
      ) : (
        <NumField
          label="Expected monthly rent"
          value={state.expected_monthly_rent_czk}
          step="500"
          placeholder="—"
          suffix="Kč/mo"
          onChange={(v) =>
            set('expected_monthly_rent_czk', v != null ? Math.round(v) : null)
          }
          hint="Optional. Adds reverse gross yield % to the result."
        />
      )}

      <div className="flex items-center gap-3 pt-1">
        <button
          type="submit"
          disabled={!valid || pending}
          className={[
            'px-4 py-2 text-sm rounded-[var(--radius-sm)] border transition-colors',
            !valid || pending
              ? 'bg-[var(--color-rule-strong)] text-[var(--color-ink-4)] border-[var(--color-rule-strong)] cursor-not-allowed'
              : 'bg-[var(--color-copper)] text-white border-[var(--color-copper)] hover:bg-[var(--color-copper-2)] hover:border-[var(--color-copper-2)]',
          ].join(' ')}
        >
          {pending ? 'Re-running…' : 'Re-run with edits'}
        </button>
        {!valid && (
          <span className="text-[0.78rem] text-[var(--color-ink-3)]">
            Latitude, longitude, area, and disposition are required.
          </span>
        )}
      </div>
    </form>
  );
}

function NumField({
  label,
  value,
  step,
  placeholder,
  suffix,
  required,
  hint,
  onChange,
}: {
  label: string;
  value: number | null;
  step?: string;
  placeholder?: string;
  suffix?: string;
  required?: boolean;
  hint?: string;
  onChange: (v: number | null) => void;
}) {
  return (
    <div>
      <FieldLabel required={required}>{label}</FieldLabel>
      <div className="mt-1.5 flex items-stretch gap-2 min-w-0">
        <input
          type="text"
          inputMode="decimal"
          value={value == null ? '' : String(value)}
          placeholder={placeholder}
          step={step}
          onChange={(e) => {
            const raw = e.target.value.trim().replace(',', '.');
            if (raw === '') return onChange(null);
            const n = Number(raw);
            if (Number.isFinite(n)) onChange(n);
          }}
          className="flex-1 min-w-0 px-3 py-2 text-sm font-mono tabular-nums rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)]"
        />
        {suffix && (
          <span className="self-center text-[0.78rem] tracking-wide text-[var(--color-ink-3)]">
            {suffix}
          </span>
        )}
      </div>
      {hint && (
        <p className="mt-1.5 text-[0.7rem] text-[var(--color-ink-4)] leading-relaxed">
          {hint}
        </p>
      )}
    </div>
  );
}

function FieldLabel({
  required,
  children,
}: {
  required?: boolean;
  children: React.ReactNode;
}) {
  return (
    <p className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
      {children}
      {required && <span className="ml-1 text-[var(--color-ink-4)]">·</span>}
    </p>
  );
}

function SegRow<T extends string>({
  options,
  value,
  onChange,
}: {
  options: ReadonlyArray<{ value: T; label: string }>;
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div
      className="mt-1.5 grid gap-1"
      style={{ gridTemplateColumns: `repeat(${options.length}, minmax(0, 1fr))` }}
    >
      {options.map((opt) => (
        <PickButton
          key={opt.value}
          on={value === opt.value}
          onClick={() => onChange(opt.value)}
        >
          {opt.label}
        </PickButton>
      ))}
    </div>
  );
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      width="10" height="10" viewBox="0 0 10 10" aria-hidden
      style={{
        transform: open ? 'rotate(180deg)' : 'rotate(0deg)',
        transition: 'transform 120ms ease',
      }}
    >
      <polyline
        points="1.5,3.5 5,7 8.5,3.5"
        stroke="currentColor"
        strokeWidth="1.25"
        fill="none"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function buildRerunPayload(
  run: EstimationRun,
  overrides?: RerunOverrides,
): CreateEstimationIn {
  const estimateKind =
    overrides?.estimate_kind ?? run.estimate_kind ?? 'rent';
  const mode = estimateKind === 'rent' ? 'agent' : 'deterministic';
  const purchasePrice =
    overrides?.purchase_price_czk !== undefined
      ? overrides.purchase_price_czk
      : run.input_purchase_price_czk;
  const expectedRent =
    overrides?.expected_monthly_rent_czk !== undefined
      ? overrides.expected_monthly_rent_czk
      : null;

  const base: CreateEstimationIn = {
    source: 'ui',
    mode,
    estimate_kind: estimateKind,
    parent_run_id: run.id,
    rerun_reason: overrides ? 'adjust' : 'manual',
    purchase_price_czk: purchasePrice,
    expected_monthly_rent_czk: expectedRent,
    /* Carry operator inputs forward on a re-run. The new row stores its
     * own copy; the original stays untouched (audit invariant). */
    special_instructions: run.special_instructions ?? null,
    contextual_text: run.contextual_text ?? null,
    ...(overrides?.provider ? { provider: overrides.provider } : {}),
    ...(overrides?.population ? { population: overrides.population } : {}),
  };

  if (overrides?.spec) {
    return { ...base, spec: overrides.spec };
  }
  if (run.input_url) {
    return { ...base, url: run.input_url };
  }
  return { ...base, spec: (run.input_spec ?? undefined) as TargetSpecIn | undefined };
}

/* -------------------------------------------------------------------------- */
/* 404 / not-found                                                            */
/* -------------------------------------------------------------------------- */

function NotFoundState({ reason, id }: { reason: 'invalid' | 'missing'; id: string | null }) {
  const headline = reason === 'invalid'
    ? "That doesn't look like a run id."
    : `No estimation #${id ?? '?'} in our database.`;
  return (
    <Page>
      <Crumb />
      <div className="mt-12 text-center">
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          Not found
        </p>
        <h1
          className="mt-2 text-2xl"
          style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
        >
          {headline}
        </h1>
        <Link
          to="/estimations"
          className="mt-4 inline-block text-sm text-[var(--color-copper)] hover:underline underline-offset-2"
        >
          Browse all estimations →
        </Link>
      </div>
    </Page>
  );
}
