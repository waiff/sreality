import { Link, useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery } from '@tanstack/react-query';
import {
  estimationKeys,
  fetchEstimation,
  submitEstimation,
} from '@/lib/queries';
import {
  fmtAbsolute,
  fmtArea,
  fmtCzk,
  fmtRelative,
} from '@/lib/format';
import { ApiError } from '@/lib/api';
import RangeStrip from '@/components/region/RangeStrip';
import Timeline from '@/components/estimation/Timeline';
import type {
  ComparableUsed,
  Confidence,
  CreateEstimationIn,
  EstimationRun,
  EstimationSource,
  TargetSpecIn,
} from '@/lib/types';

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

  const rerunMut = useMutation<EstimationRun, ApiError, EstimationRun>({
    mutationFn: (run) => submitEstimation(buildRerunPayload(run)),
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

      <SectionLabel>Trace</SectionLabel>
      <div className="mt-4">
        <Timeline trace={run.trace} />
      </div>

      {!isFailed && (
        <>
          <Hairline />
          <ComparablesBlock comps={run.comparables_used ?? []} />
        </>
      )}

      <Hairline />
      <RerunBlock
        run={run}
        onRerun={() => rerunMut.mutate(run)}
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
/* Comparables table                                                          */
/* -------------------------------------------------------------------------- */

function ComparablesBlock({ comps }: { comps: ComparableUsed[] }) {
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
  const sorted = [...comps].sort(
    (a, b) => (a.data_age_days ?? Infinity) - (b.data_age_days ?? Infinity),
  );
  return (
    <div>
      <div className="flex items-baseline justify-between">
        <SectionLabel>Comparables</SectionLabel>
        <p className="text-[0.7rem] tracking-wide text-[var(--color-ink-4)] font-mono tabular-nums">
          {comps.length}
        </p>
      </div>
      <div className="mt-3 rounded-[var(--radius-md)] border border-[var(--color-rule)] overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-[var(--color-paper-2)] border-b border-[var(--color-rule)]">
            <tr>
              <Th align="left">ID</Th>
              <Th align="left">Snapshot</Th>
              <Th align="right">Age</Th>
              <Th align="left">Verified</Th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((c) => (
              <tr
                key={`${c.sreality_id}:${c.snapshot_id ?? 'none'}`}
                className="border-b border-[var(--color-rule-soft)] last:border-b-0 hover:bg-[var(--color-copper-soft)]/40 transition-colors"
              >
                <td className="px-3 py-2 align-middle">
                  <Link
                    to={`/listing/${c.sreality_id}`}
                    className="font-mono tabular-nums text-[var(--color-copper)] hover:underline underline-offset-2"
                  >
                    {c.sreality_id}
                  </Link>
                </td>
                <td className="px-3 py-2 align-middle font-mono tabular-nums text-[var(--color-ink-2)]">
                  {c.snapshot_date ? c.snapshot_date.slice(0, 10) : '—'}
                </td>
                <td className="px-3 py-2 align-middle text-right font-mono tabular-nums text-[var(--color-ink)]">
                  {c.data_age_days != null ? `${c.data_age_days} d` : '—'}
                </td>
                <td className="px-3 py-2 align-middle text-[0.78rem]">
                  {c.verified_during_estimate ? (
                    <span className="text-[var(--color-sage)]">verified</span>
                  ) : (
                    <span className="text-[var(--color-ink-4)]">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
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

function RerunBlock({
  run,
  onRerun,
  pending,
  error,
}: {
  run: EstimationRun;
  onRerun: () => void;
  pending: boolean;
  error: ApiError | null;
}) {
  const canRerun = run.input_url != null || run.input_spec != null;
  return (
    <div>
      <SectionLabel>Re-run</SectionLabel>
      <p className="mt-2 text-[0.78rem] text-[var(--color-ink-3)] leading-relaxed">
        Re-runs use the same inputs as this run and link back via parent_run_id.
        The original record is immutable.
      </p>
      <div className="mt-3 flex items-center gap-3">
        <button
          type="button"
          disabled={!canRerun || pending}
          onClick={onRerun}
          className={[
            'px-4 py-2 text-sm rounded-[var(--radius-sm)] border transition-colors',
            !canRerun || pending
              ? 'bg-[var(--color-rule-strong)] text-[var(--color-ink-4)] border-[var(--color-rule-strong)] cursor-not-allowed'
              : 'bg-[var(--color-paper-2)] text-[var(--color-ink-2)] border-[var(--color-rule)] hover:border-[var(--color-copper)] hover:text-[var(--color-copper)]',
          ].join(' ')}
        >
          {pending ? 'Re-running…' : 'Re-run with same inputs'}
        </button>
        {!canRerun && (
          <span className="text-[0.78rem] text-[var(--color-ink-3)]">
            Original inputs unavailable.
          </span>
        )}
      </div>
      {error && (
        <p className="mt-2 text-[0.78rem] text-[var(--color-brick)]">
          {error.message || `Re-run failed (HTTP ${error.status}).`}
        </p>
      )}
    </div>
  );
}

function buildRerunPayload(run: EstimationRun): CreateEstimationIn {
  const base: CreateEstimationIn = {
    source: 'ui',
    estimate_kind: run.estimate_kind ?? 'rent',
    parent_run_id: run.id,
    rerun_reason: 'manual',
    purchase_price_czk: run.input_purchase_price_czk,
  };
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
