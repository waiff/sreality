import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState, type MouseEvent } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  estimationKeys,
  fetchEstimation,
  fetchImagesByListing,
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
import {
  ApiError,
  decideRefinement,
  fetchListingSummaries,
  getSkill,
  listEstimationFeedback,
  patchEstimationScenario,
  submitEstimationFeedback,
  updateSkill,
  type FeedbackResponse,
  type Skill,
  type YieldScenarioUpdate,
} from '@/lib/api';
import RangeStrip from '@/components/region/RangeStrip';
import Timeline from '@/components/estimation/Timeline';
import { PickButton } from '@/components/controls';
import {
  buildRerunPayload,
  canRerun,
  type RerunInput,
  type RerunOverrides,
} from '@/lib/rerun';
import { imageSrc } from '@/lib/imageUrl';
import type {
  ComparableExcluded,
  ComparableUsed,
  Confidence,
  Disposition,
  EstimationFeedback,
  EstimationProvider,
  EstimationRun,
  EstimationSource,
  FeedbackStatus,
  ImagePublic,
  ListingPublic,
  ListingSummaryBatchRow,
  Population,
  SkillRefinement,
  SubjectSummary,
  Trace,
} from '@/lib/types';

const ComparablesMap = lazy(
  () => import('@/components/estimation/ComparablesMap'),
);
const ComparableModal = lazy(
  () => import('@/components/estimation/ComparableModal'),
);

/* Shared by Header (image carousel) and YieldBlock (sale-price prefill).
 * Hoisted to the EstimationDetail body so both consumers read from one
 * React Query result instead of re-fetching the same row. */
function useSubjectListing(run: EstimationRun | null) {
  const id = run?.input_sreality_id;
  return useQuery<ListingPublic | null, Error>({
    queryKey: ['estimation-subject-listing', id],
    queryFn: () => fetchListingById(id as number),
    enabled: id != null,
    staleTime: 60_000,
  });
}

function useSubjectImages(run: EstimationRun | null, enabled: boolean) {
  const id = run?.input_sreality_id;
  return useQuery<ImagePublic[], Error>({
    queryKey: ['estimation-subject-images', id],
    queryFn: () => fetchImagesByListing(id as number),
    enabled: enabled && id != null,
    staleTime: 60_000,
  });
}


export default function EstimationDetail() {
  const { id: idParam } = useParams();
  const navigate = useNavigate();
  const id = idParam && /^\d+$/.test(idParam) ? Number(idParam) : null;

  const runQ = useQuery<EstimationRun, Error>({
    queryKey: id != null ? estimationKeys.detail(id) : ['estimations', 'detail', null],
    queryFn: () => fetchEstimation(id as number),
    enabled: id != null,
    staleTime: 60_000,
    refetchInterval: (q) => {
      const status = q.state.data?.status;
      return status === 'pending' || status === 'running' ? 2000 : false;
    },
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
  const isInFlight = run.status === 'pending' || run.status === 'running';

  return (
    <EstimationDetailBody
      run={run}
      isFailed={isFailed}
      isInFlight={isInFlight}
      rerunMut={rerunMut}
    />
  );
}

function EstimationDetailBody({
  run, isFailed, isInFlight, rerunMut,
}: {
  run: EstimationRun;
  isFailed: boolean;
  isInFlight: boolean;
  rerunMut: ReturnType<typeof useMutation<EstimationRun, ApiError, RerunInput>>;
}) {
  const subjectQ = useSubjectListing(run);
  const imagesQ = useSubjectImages(run, subjectQ.data != null);

  if (isInFlight) {
    return (
      <Page>
        <Crumb />
        <Header run={run} subject={subjectQ.data ?? null} images={imagesQ.data ?? []} />
        <Hairline />
        <InFlightBlock run={run} />
        <Hairline />
        <InputRecap run={run} />
      </Page>
    );
  }

  return (
    <Page>
      <Crumb />
      <Header run={run} subject={subjectQ.data ?? null} images={imagesQ.data ?? []} />

      {!isFailed && (
        <>
          <Hairline />
          <YieldBlock run={run} subject={subjectQ.data ?? null} />
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

      <Hairline />
      <FeedbackBlock runId={run.id} />

      <FloatingFeedbackPanel runId={run.id} run={run} />
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

/* The header is the case-file cover for an estimation. Left column is a
 * photo dossier (carousel of listing photos, same chevron/counter chrome
 * as the browse cards). Right column carries the run identity, the big
 * estimated headline, and the listing facts that tell the operator what
 * they're looking at. Badges (confidence + source) ride the top-right
 * corner. Falls back gracefully when the subject listing isn't in our DB
 * yet — the right column degrades to just the estimate number + run
 * metadata, and the photo well shows a neutral placeholder. */
function Header({
  run, subject, images,
}: {
  run: EstimationRun;
  subject: ListingPublic | null;
  images: ImagePublic[];
}) {
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
    <div className="mt-5">
      <div className="grid gap-5 sm:gap-6 sm:grid-cols-[minmax(0,260px),1fr]">
        <SubjectImageStrip
          images={images}
          subject={subject}
          spec={run.input_spec ?? null}
          inputUrl={run.input_url}
        />

        <div className="min-w-0 flex flex-col">
          <div className="flex items-start justify-between gap-4">
            <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
              Estimation · run #{run.id}
            </p>
            <div className="shrink-0 flex flex-col items-end gap-1.5">
              {!failed && <ConfidencePill confidence={run.confidence} />}
              <SourceBadge source={run.source} />
            </div>
          </div>
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

          <SubjectIdentity
            run={run}
            subject={subject}
          />
        </div>
      </div>
    </div>
  );
}

/* "Photo dossier" — the photo strip that pins this estimation to a
 * tangible listing. Carousel chrome is the same vocabulary the browse
 * cards use (translucent paper-3 chevrons + tabular counter), keeping
 * the gestural language consistent across the app. When there are no
 * photos (listing not yet in our DB, or images table empty), the well
 * shows a quiet placeholder rather than a broken-photo glyph — the
 * absence is information, not an error. */
function SubjectImageStrip({
  images, subject, spec, inputUrl,
}: {
  images: ImagePublic[];
  subject: ListingPublic | null;
  spec: EstimationRun['input_spec'];
  inputUrl: string | null;
}) {
  const [index, setIndex] = useState(0);
  const urls = useMemo(() => images.map(imageSrc), [images]);
  const safeIndex = urls.length === 0 ? 0 : Math.min(index, urls.length - 1);
  const hasMany = urls.length > 1;

  const step = useCallback(
    (delta: number) => (e: MouseEvent) => {
      e.preventDefault();
      e.stopPropagation();
      if (urls.length === 0) return;
      setIndex((cur) => (cur + delta + urls.length) % urls.length);
    },
    [urls.length],
  );

  const inactive = subject ? !subject.is_active : false;
  const sid = subject?.sreality_id;

  const placeholderText = subject
    ? 'no photos'
    : inputUrl
      ? 'listing not yet in archive'
      : spec
        ? 'spec-only estimation'
        : 'no subject listing';

  const content = (
    <div
      className={[
        'aspect-[5/4] w-full overflow-hidden relative rounded-[var(--radius-sm)]',
        'border border-[var(--color-rule)] bg-[var(--color-inset)]',
      ].join(' ')}
    >
      {urls.length > 0 ? (
        <img
          src={urls[safeIndex]}
          alt=""
          loading="lazy"
          className={[
            'w-full h-full object-cover',
            inactive ? 'saturate-[0.55] brightness-[0.95]' : '',
          ].join(' ')}
          onError={(e) => {
            (e.currentTarget as HTMLImageElement).style.visibility = 'hidden';
          }}
        />
      ) : (
        <div className="w-full h-full flex items-center justify-center px-4 text-center text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
          {placeholderText}
        </div>
      )}

      {subject && (
        <div className="absolute top-1 right-1 flex flex-col items-end gap-1">
          <span
            className={[
              'inline-flex items-center px-1.5 py-0.5 text-[0.6rem] tracking-[0.12em]',
              'uppercase rounded-[var(--radius-xs)] border backdrop-blur-sm font-medium',
              inactive
                ? 'bg-[var(--color-paper-3)]/90 border-[var(--color-brick)]/70 text-[var(--color-brick)]'
                : 'bg-[var(--color-paper-3)]/90 border-[var(--color-sage)]/70 text-[var(--color-sage)]',
            ].join(' ')}
          >
            {inactive ? 'Neaktivní' : 'Aktivní'}
          </span>
        </div>
      )}

      {hasMany && (
        <>
          <button
            type="button"
            onClick={step(-1)}
            aria-label="Previous photo"
            className="absolute top-1/2 left-1 -translate-y-1/2 w-6 h-6 flex items-center justify-center rounded-full bg-[var(--color-paper-3)]/85 border border-[var(--color-rule)] text-[var(--color-ink-2)] backdrop-blur-sm hover:text-[var(--color-copper)] hover:border-[var(--color-rule-strong)] transition-colors"
          >
            <CarouselChevron dir="left" />
          </button>
          <button
            type="button"
            onClick={step(1)}
            aria-label="Next photo"
            className="absolute top-1/2 right-1 -translate-y-1/2 w-6 h-6 flex items-center justify-center rounded-full bg-[var(--color-paper-3)]/85 border border-[var(--color-rule)] text-[var(--color-ink-2)] backdrop-blur-sm hover:text-[var(--color-copper)] hover:border-[var(--color-rule-strong)] transition-colors"
          >
            <CarouselChevron dir="right" />
          </button>
          <span className="absolute bottom-1 right-1 px-1.5 py-0.5 text-[0.6rem] tracking-[0.08em] tabular-nums rounded-[var(--radius-xs)] bg-[var(--color-paper-3)]/85 border border-[var(--color-rule)] text-[var(--color-ink-2)] backdrop-blur-sm">
            {safeIndex + 1} / {urls.length}
          </span>
        </>
      )}
    </div>
  );

  if (sid != null) {
    return (
      <Link
        to={`/listing/${sid}`}
        className="block group focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-focus)] rounded-[var(--radius-sm)]"
        title="Open listing detail"
      >
        {content}
      </Link>
    );
  }
  return content;
}

function CarouselChevron({ dir }: { dir: 'left' | 'right' }) {
  const d = dir === 'left' ? 'M7.5 3 L4 6 L7.5 9' : 'M4.5 3 L8 6 L4.5 9';
  return (
    <svg
      width="12" height="12" viewBox="0 0 12 12" aria-hidden fill="none"
      stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"
    >
      <path d={d} />
    </svg>
  );
}

/* Listing identity — the "what is this estimation about" line(s). Pulls
 * from the persisted listings_public row when available; falls back to
 * the spec stored on the run so spec-only estimations still read
 * cleanly. Title is the Czech taxonomy ("Byt 2+kk · 71 m²"), one line
 * for locality, one mono row for sale price + price/m² when those make
 * sense. Goes silent (returns null) when there is genuinely nothing to
 * show beyond the spec lat/lng. */
function SubjectIdentity({
  run, subject,
}: {
  run: EstimationRun;
  subject: ListingPublic | null;
}) {
  const spec = run.input_spec ?? null;

  const categoryMain = subject?.category_main ?? null;
  const categoryType = subject?.category_type ?? null;
  const disposition = subject?.disposition ?? spec?.disposition ?? null;
  const area = subject?.area_m2 ?? spec?.area_m2 ?? null;
  const floor = subject?.floor ?? spec?.floor ?? null;
  const totalFloors = subject?.total_floors ?? null;

  const title = formatListingTitle(categoryMain, categoryType, disposition, area);
  const place = subject
    ? [subject.locality, subject.district].filter(Boolean).join(', ')
    : null;

  const isSale = categoryType === 'prodej';
  const isRent = categoryType === 'pronajem';
  const showListingPrice = subject?.price_czk != null;
  const listingPriceLabel = isSale
    ? 'sale price'
    : isRent
      ? 'monthly rent'
      : 'list price';
  const ppm2 =
    subject?.price_czk != null && subject.area_m2 != null
      ? fmtPricePerM2(subject.price_czk, subject.area_m2)
      : null;

  const floorLine =
    floor != null
      ? totalFloors != null
        ? `${floor}. patro / ${totalFloors}`
        : `${floor}. patro`
      : null;

  if (!title && !place && !showListingPrice && !floorLine) return null;

  return (
    <div className="mt-4 pt-4 border-t border-[var(--color-rule)]">
      {title && (
        <h2
          className="text-[1rem] leading-snug text-[var(--color-ink)]"
          style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
        >
          {title}
        </h2>
      )}
      {place && (
        <p className="mt-0.5 text-[0.82rem] text-[var(--color-ink-2)] truncate">
          {place}
        </p>
      )}
      <div className="mt-2 flex flex-wrap items-baseline gap-x-4 gap-y-1 text-[0.78rem] font-mono tabular-nums text-[var(--color-ink-2)]">
        {showListingPrice && (
          <span>
            <span className="text-[0.65rem] tracking-[0.12em] uppercase text-[var(--color-ink-4)] mr-1">
              {listingPriceLabel}
            </span>
            <span className="text-[var(--color-ink)]">{fmtCzk(subject!.price_czk)}</span>
          </span>
        )}
        {ppm2 && (
          <span className="text-[var(--color-ink-3)]">{ppm2}</span>
        )}
        {floorLine && (
          <span className="text-[var(--color-ink-3)]">{floorLine}</span>
        )}
      </div>
    </div>
  );
}

function formatListingTitle(
  categoryMain: string | null,
  categoryType: string | null,
  disposition: string | null,
  areaM2: number | null,
): string | null {
  const kind = (() => {
    if (categoryMain === 'byt') return 'Byt';
    if (categoryMain === 'dum') return 'Dům';
    if (categoryMain === 'komercni') return 'Komerční prostor';
    return null;
  })();
  const deal =
    categoryType === 'pronajem'
      ? 'k pronájmu'
      : categoryType === 'prodej'
        ? 'na prodej'
        : null;
  const parts: string[] = [];
  if (kind && deal) parts.push(`${kind} ${deal}`);
  else if (kind) parts.push(kind);
  else if (disposition || areaM2 != null) parts.push('Nemovitost');
  if (disposition) parts.push(disposition);
  if (areaM2 != null) parts.push(fmtArea(areaM2));
  return parts.length > 0 ? parts.join(' · ') : null;
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

function YieldBlock({
  run, subject,
}: {
  run: EstimationRun;
  subject: ListingPublic | null;
}) {
  const kind = run.estimate_kind ?? 'rent';
  const areaM2 = run.input_spec?.area_m2 ?? null;
  const defaultRent = run.estimated_monthly_rent_czk;

  const subjectSalePrice =
    subject && subject.category_type === 'prodej' ? subject.price_czk : null;

  const defaultPrice =
    subjectSalePrice ??
    run.input_purchase_price_czk ??
    (kind === 'sale' ? run.estimated_sale_price_czk : null);

  /* Scenario state lives on estimation_runs.scenario (migration 085)
   * and is shared with the Chrome extension. Initial values come from
   * run.scenario; a non-null field marks that axis as "touched" (owned
   * by the operator's edit) so the auto-sync to default below skips it. */
  const persistedRent = run.scenario?.rent_czk ?? null;
  const persistedCost = run.scenario?.fond_per_m2_czk ?? null;
  const persistedPrice = run.scenario?.price_czk ?? null;

  const [rent, setRent] = useState<number | null>(
    persistedRent !== null ? persistedRent : defaultRent,
  );
  const [costPerM2, setCostPerM2] = useState<number | null>(
    persistedCost !== null ? persistedCost : DEFAULT_FOND_CZK_PER_M2,
  );
  const [price, setPriceState] = useState<number | null>(
    persistedPrice !== null ? persistedPrice : defaultPrice,
  );
  const [priceTouched, setPriceTouched] = useState<boolean>(
    persistedPrice !== null,
  );
  const [rentTouched, setRentTouched] = useState<boolean>(
    persistedRent !== null,
  );
  const [costTouched, setCostTouched] = useState<boolean>(
    persistedCost !== null,
  );

  /* Sync untouched inputs to the latest defaults — handles the subject
   * listing query resolving after first render, or a defaultRent
   * arriving in a refetch. Once the operator types into a field that
   * field is "owned" by their entry and stops following the default. */
  useEffect(() => {
    if (!priceTouched) setPriceState(defaultPrice);
  }, [defaultPrice, priceTouched]);

  const qc = useQueryClient();
  const runId = run.id;
  const scenarioMut = useMutation<EstimationRun, ApiError, YieldScenarioUpdate>({
    mutationFn: (body) => patchEstimationScenario(runId, body),
    onSuccess: (updated) => {
      qc.setQueryData<EstimationRun>(estimationKeys.detail(runId), updated);
    },
  });

  /* Debounce keystrokes so the PATCH fires once a beat after the
   * operator stops typing. 500ms matches the feel of the SPA's other
   * inline edits; trades off latency for not hammering the API. */
  useEffect(() => {
    const handle = window.setTimeout(() => {
      scenarioMut.mutate({
        rent_czk: rentTouched ? rent : null,
        fond_per_m2_czk: costTouched ? costPerM2 : null,
        price_czk: priceTouched ? price : null,
      });
    }, 500);
    return () => window.clearTimeout(handle);
    /* scenarioMut is stable across renders for our purposes; depending
     * on it would refire the timer when the mutation state churns. */
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rent, costPerM2, price, priceTouched, rentTouched, costTouched]);

  const setPrice = (v: number | null) => {
    setPriceTouched(true);
    setPriceState(v);
  };
  const setRentTouching = (v: number | null) => {
    setRentTouched(true);
    setRent(v);
  };
  const setCostTouching = (v: number | null) => {
    setCostTouched(true);
    setCostPerM2(v);
  };

  const resetScenario = () => {
    setPriceTouched(false);
    setRentTouched(false);
    setCostTouched(false);
    setRent(defaultRent);
    setCostPerM2(DEFAULT_FOND_CZK_PER_M2);
    setPriceState(defaultPrice);
    /* The debounce useEffect will pick up the touched=false transitions
     * and PATCH with all-null automatically. No direct mutate here. */
  };

  const hasOverrides = priceTouched || rentTouched || costTouched;

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
      <div className="flex items-baseline justify-between gap-3">
        <SectionLabel>Yield</SectionLabel>
        <div className="flex items-baseline gap-3">
          {hasOverrides && (
            <button
              type="button"
              onClick={resetScenario}
              className="text-[0.7rem] tracking-wide uppercase text-[var(--color-copper)] hover:text-[var(--color-copper-2)] hover:underline underline-offset-2 transition-colors"
              title="Discard edits and restore the defaults"
            >
              Reset
            </button>
          )}
          <p className="text-[0.7rem] tracking-wide text-[var(--color-ink-4)]">
            {hasOverrides ? 'edited · synced' : 'live calculation'}
          </p>
        </div>
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
          onChange={setRentTouching}
          hint={defaultRent != null ? 'Default: median estimate' : 'No estimate — set manually'}
        />
        <YieldNumField
          label="Fond oprav + SVJ"
          value={costPerM2}
          step="1"
          suffix="Kč/m²"
          onChange={setCostTouching}
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
/* In-flight (pending / running)                                              */
/* -------------------------------------------------------------------------- */

function InFlightBlock({ run }: { run: EstimationRun }) {
  const label =
    run.status === 'pending' ? 'Queued' : 'Estimating';
  return (
    <div className="flex items-start gap-3">
      <span
        aria-hidden
        className="mt-1.5 inline-block h-2 w-2 rounded-full bg-[var(--color-copper)] animate-pulse"
      />
      <div>
        <SectionLabel>{label}</SectionLabel>
        <p className="mt-2 text-[0.95rem] text-[var(--color-ink)]">
          {run.mode === 'agent'
            ? 'The agent is working through comparables and refining its estimate.'
            : 'Pulling comparables and computing the rent distribution.'}
        </p>
        <p className="mt-2 text-[0.8rem] text-[var(--color-ink-3)]">
          You can navigate away — this page auto-updates when the run
          completes. Started {fmtRelative(run.created_at)}.
        </p>
      </div>
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

  // Agent-mode runs record the skill choice as the first
  // computation step of the trace. Pulling skill / model / provider
  // out of it answers "why this skill" — the row is the audit
  // truth, and the Timeline below renders the full details on
  // expand.
  const skillChoice = pickSkillChoiceFromTrace(run);
  if (skillChoice) {
    facts.push(['Mode', run.mode]);
    facts.push(['Skill', skillChoice.skill_name]);
    facts.push(['Model', `${skillChoice.provider} / ${skillChoice.model}`]);
  } else if (run.mode) {
    facts.push(['Mode', run.mode]);
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

interface SkillChoiceSummary {
  skill_name: string;
  provider: string;
  model: string;
}

function pickSkillChoiceFromTrace(run: EstimationRun): SkillChoiceSummary | null {
  const steps = run.trace?.steps ?? [];
  for (const step of steps) {
    if (step.kind !== 'computation') continue;
    const label = (step as { label?: unknown }).label;
    if (label !== 'skill_choice') continue;
    const out = (step.output_summary ?? {}) as Record<string, unknown>;
    const skill_name = out.skill_name;
    const provider = out.provider;
    const model = out.model;
    if (
      typeof skill_name === 'string' &&
      typeof provider === 'string' &&
      typeof model === 'string'
    ) {
      return { skill_name, provider, model };
    }
    return null;
  }
  return null;
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
              <Th align="left">Why kept</Th>
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

      <ExcludedComparables excluded={run.comparables_excluded} />

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
      <td className="px-3 py-2 align-middle text-[0.82rem] text-[var(--color-ink-2)]">
        {comp.reason
          ? <span className="line-clamp-2 leading-snug italic">{comp.reason}</span>
          : <span className="text-[var(--color-ink-4)]">—</span>}
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

function ExcludedComparables({
  excluded,
}: {
  excluded: ComparableExcluded[] | null;
}) {
  if (!excluded || excluded.length === 0) return null;
  return (
    <div className="mt-6">
      <div className="flex items-baseline justify-between">
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
          Considered and set aside
        </p>
        <p className="text-[0.7rem] tracking-wide text-[var(--color-ink-4)] font-mono tabular-nums">
          {excluded.length}
        </p>
      </div>
      <ul className="mt-3 space-y-1.5">
        {excluded.map((row) => (
          <li
            key={row.sreality_id}
            className="flex items-baseline gap-3 text-[0.82rem] text-[var(--color-ink-2)] px-3 py-1.5 rounded-[var(--radius-xs)] border border-[var(--color-rule-soft)] bg-[var(--color-paper-2)]"
          >
            <Link
              to={`/listing/${row.sreality_id}`}
              className="shrink-0 font-mono tabular-nums text-[var(--color-copper)] hover:underline underline-offset-2"
            >
              {row.sreality_id}
            </Link>
            <span className="italic leading-snug">{row.reason}</span>
          </li>
        ))}
      </ul>
    </div>
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
  const rerunnable = canRerun(run);
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
          disabled={!rerunnable || pending}
          onClick={() => onRerun()}
          className={[
            'px-4 py-2 text-sm rounded-[var(--radius-sm)] border transition-colors',
            !rerunnable || pending
              ? 'bg-[var(--color-rule-strong)] text-[var(--color-ink-4)] border-[var(--color-rule-strong)] cursor-not-allowed'
              : 'bg-[var(--color-paper-2)] text-[var(--color-ink-2)] border-[var(--color-rule)] hover:border-[var(--color-copper)] hover:text-[var(--color-copper)]',
          ].join(' ')}
        >
          {pending ? 'Re-running…' : 'Re-run with same inputs'}
        </button>

        {rerunnable && run.input_spec && (
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

        {!rerunnable && (
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

/* -------------------------------------------------------------------------- */
/* Feedback (Phase AI slice B + C)                                            */
/* -------------------------------------------------------------------------- */

const feedbackKeys = {
  list: (runId: number) =>
    ['estimations', 'detail', runId, 'feedback'] as const,
};

function FeedbackBlock({ runId }: { runId: number }) {
  const qc = useQueryClient();
  const location = useLocation();
  const openedViaHash = location.hash === '#feedback';

  const listQ = useQuery<{ data: EstimationFeedback[] }, ApiError>({
    queryKey: feedbackKeys.list(runId),
    queryFn: () => listEstimationFeedback(runId),
    staleTime: 30_000,
  });

  const decideMut = useMutation<
    SkillRefinement,
    ApiError,
    { refinementId: number; decision: 'apply' | 'dismiss' }
  >({
    mutationFn: ({ refinementId, decision }) =>
      decideRefinement(refinementId, decision),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: feedbackKeys.list(runId) });
    },
  });

  const rows = listQ.data?.data ?? [];

  const scrolledRef = useRef(false);
  useEffect(() => {
    if (!openedViaHash) return;
    if (!listQ.isSuccess) return;
    if (scrolledRef.current) return;
    scrolledRef.current = true;
    const raf = requestAnimationFrame(() => {
      document
        .getElementById('feedback')
        ?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
    return () => cancelAnimationFrame(raf);
  }, [openedViaHash, listQ.isSuccess]);

  const autoExpandRefinementId = useMemo(() => {
    if (!openedViaHash) return null;
    const target = rows.find(
      (r) => r.status === 'proposed' && r.refinement_id != null,
    );
    return target?.refinement_id ?? null;
  }, [openedViaHash, rows]);

  return (
    <div id="feedback">
      <SectionLabel>Feedback history</SectionLabel>
      <p className="mt-2 text-[0.78rem] text-[var(--color-ink-3)] leading-relaxed">
        Previous notes on this run and the prompt edits they triggered.
        Use the <span className="text-[var(--color-copper)]">Provide
        feedback</span> button pinned at the top of the page to add a
        new note.
      </p>

      <FeedbackHistory
        rows={rows}
        loading={listQ.isLoading}
        decidePending={decideMut.isPending}
        onDecide={(refinementId, decision) =>
          decideMut.mutate({ refinementId, decision })
        }
        decideError={decideMut.error}
        autoExpandRefinementId={autoExpandRefinementId}
      />
    </div>
  );
}

function pickSkillNameFromTrace(trace: Trace | null): string | null {
  if (!trace) return null;
  for (const step of trace.steps ?? []) {
    if (step.kind !== 'computation') continue;
    if (step.label !== 'skill_choice') continue;
    const name = step.output_summary?.skill_name;
    return typeof name === 'string' && name.length > 0 ? name : null;
  }
  return null;
}

/* Schema default in api/schemas.CreateEstimationIn — used as the
 * skill-editor fallback when the run's trace pre-dates the
 * skill_choice step or when the run was deterministic. */
const DEFAULT_SKILL_NAME = 'rental_estimator_full_v1';

/* Positions the floating panel column to the right of the centered
 * `max-w-3xl` page body. Built so the panel fills the *residual*
 * horizontal space when there is one, and falls back to a usable
 * right-pinned width on narrower viewports.                       */
const PANEL_LEFT = 'max(1rem, calc(50% + 24rem + 1rem))';
const PANEL_RIGHT = '1rem';
const PANEL_TOP = '3.75rem';
const PANEL_MAX_HEIGHT = 'calc(100dvh - 4.5rem)';

function FloatingFeedbackPanel({
  runId,
  run,
}: {
  runId: number;
  run: EstimationRun;
}) {
  const qc = useQueryClient();

  const skillNameFromTrace = useMemo(
    () => pickSkillNameFromTrace(run.trace),
    [run.trace],
  );
  const effectiveSkillName = skillNameFromTrace ?? DEFAULT_SKILL_NAME;
  const skillSource: 'trace' | 'default' =
    skillNameFromTrace != null ? 'trace' : 'default';

  const [open, setOpen] = useState(false);
  const [text, setText] = useState('');
  const [kickOff, setKickOff] = useState(true);
  const [promptDraft, setPromptDraft] = useState<string | null>(null);

  const skillQ = useQuery<Skill, ApiError>({
    queryKey: ['admin', 'skills', effectiveSkillName],
    queryFn: () => getSkill(effectiveSkillName),
    enabled: open,
    staleTime: 30_000,
  });

  useEffect(() => {
    if (skillQ.data && promptDraft === null) {
      setPromptDraft(skillQ.data.system_prompt);
    }
  }, [skillQ.data, promptDraft]);

  useEffect(() => {
    setPromptDraft(null);
  }, [effectiveSkillName]);

  const promptDirty =
    skillQ.data != null &&
    promptDraft != null &&
    promptDraft !== skillQ.data.system_prompt;

  const submitMut = useMutation<
    FeedbackResponse,
    ApiError,
    { text: string; kickOff: boolean }
  >({
    mutationFn: ({ text, kickOff }) =>
      submitEstimationFeedback(runId, {
        feedback_text: text,
        kick_off_refinement: kickOff,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: feedbackKeys.list(runId) });
      setText('');
      setOpen(false);
    },
  });

  const updatePromptMut = useMutation<Skill, ApiError, { systemPrompt: string }>(
    {
      mutationFn: ({ systemPrompt }) =>
        updateSkill(effectiveSkillName, { system_prompt: systemPrompt }),
      onSuccess: (skill) => {
        qc.setQueryData(['admin', 'skills', skill.name], skill);
        setPromptDraft(skill.system_prompt);
      },
    },
  );

  const submitValid = text.trim().length > 0;

  if (!open) {
    return (
      <div
        className="fixed z-20"
        style={{ top: PANEL_TOP, left: PANEL_LEFT, right: PANEL_RIGHT }}
      >
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="inline-flex items-center gap-2 px-3 py-1.5 text-sm rounded-[var(--radius-sm)] border border-[var(--color-copper)] text-[var(--color-copper)] bg-[var(--color-paper)] hover:bg-[var(--color-copper-soft)] shadow-[0_2px_8px_rgba(0,0,0,0.06)] transition-colors"
        >
          <FeedbackGlyph />
          <span>Provide feedback</span>
        </button>
      </div>
    );
  }

  return (
    <div
      className="fixed z-20 flex flex-col gap-3 overflow-y-auto"
      style={{
        top: PANEL_TOP,
        left: PANEL_LEFT,
        right: PANEL_RIGHT,
        maxHeight: PANEL_MAX_HEIGHT,
      }}
    >
      {/* Panel 1 — feedback composer ---------------------------------- */}
      <section className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] shadow-[0_8px_24px_rgba(0,0,0,0.08)] overflow-hidden">
        <div className="px-4 py-3 border-b border-[var(--color-rule)] flex items-center justify-between gap-3">
          <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
            Feedback
          </p>
          <button
            type="button"
            onClick={() => setOpen(false)}
            aria-label="Close feedback"
            className="text-[0.78rem] tracking-wide text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)] transition-colors"
          >
            Close
          </button>
        </div>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (submitValid && !submitMut.isPending) {
              submitMut.mutate({ text: text.trim(), kickOff });
            }
          }}
          className="px-4 py-4 space-y-4"
        >
          <p className="text-[0.78rem] text-[var(--color-ink-3)] leading-relaxed">
            Tell the skill what it got wrong. The refiner reads your note
            plus this run's trace and proposes an updated system prompt
            for the skill that produced this estimate.
          </p>

          <div>
            <FieldLabel required>
              What did the skill get wrong on this run?
            </FieldLabel>
            <textarea
              rows={4}
              value={text}
              onChange={(e) => setText(e.target.value)}
              maxLength={4000}
              placeholder="e.g. The cohort was too broad — it kept three 4+kk listings even though the target is 2+kk. Tighten the disposition match before relaxing it."
              className="mt-1.5 w-full px-3 py-2 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)] resize-y"
            />
            <p className="mt-1 text-[0.7rem] text-[var(--color-ink-4)] tabular-nums">
              {text.length} / 4000
            </p>
          </div>

          <label className="flex items-baseline gap-2 text-[0.82rem] text-[var(--color-ink-2)]">
            <input
              type="checkbox"
              checked={kickOff}
              onChange={(e) => setKickOff(e.target.checked)}
            />
            <span>
              Run the refiner now (costs ~$0.05). Uncheck to stash the
              feedback for a later batch.
            </span>
          </label>

          {submitMut.error && (
            <p className="text-[0.78rem] text-[var(--color-brick)]">
              {submitMut.error.message ||
                `Submit failed (HTTP ${submitMut.error.status}).`}
            </p>
          )}

          <div className="flex items-center justify-end gap-2 pt-1">
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="px-3 py-2 text-sm rounded-[var(--radius-sm)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)] transition-colors"
            >
              Close
            </button>
            <button
              type="submit"
              disabled={!submitValid || submitMut.isPending}
              className={[
                'px-4 py-2 text-sm rounded-[var(--radius-sm)] border transition-colors',
                !submitValid || submitMut.isPending
                  ? 'bg-[var(--color-rule-strong)] text-[var(--color-ink-4)] border-[var(--color-rule-strong)] cursor-not-allowed'
                  : 'bg-[var(--color-copper)] text-white border-[var(--color-copper)] hover:bg-[var(--color-copper-2)] hover:border-[var(--color-copper-2)]',
              ].join(' ')}
            >
              {submitMut.isPending
                ? kickOff
                  ? 'Refining…'
                  : 'Saving…'
                : kickOff
                  ? 'Submit and refine'
                  : 'Save without refining'}
            </button>
          </div>
        </form>
      </section>

      {/* Panel 2 — skill prompt editor -------------------------------- */}
      <section className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] shadow-[0_8px_24px_rgba(0,0,0,0.08)] overflow-hidden">
        <div className="px-4 py-3 border-b border-[var(--color-rule)] flex items-center justify-between gap-3">
          <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
            Skill prompt · {effectiveSkillName}
          </p>
          <span className="text-[0.65rem] tracking-[0.16em] uppercase text-[var(--color-ink-4)]">
            {skillSource === 'trace'
              ? `from run trace · mode ${run.mode}`
              : run.mode === 'agent'
                ? 'default (run trace had no skill_choice)'
                : `default (run was ${run.mode})`}
          </span>
        </div>
        <div className="px-4 py-4 space-y-2">
          {skillQ.isLoading && promptDraft === null ? (
            <p className="text-[0.78rem] text-[var(--color-ink-4)] italic">
              Loading skill…
            </p>
          ) : skillQ.error ? (
            <p className="text-[0.78rem] text-[var(--color-brick)]">
              Could not load skill: {skillQ.error.message}
            </p>
          ) : (
            <>
              <textarea
                rows={12}
                value={promptDraft ?? skillQ.data?.system_prompt ?? ''}
                onChange={(e) => setPromptDraft(e.target.value)}
                className="w-full px-3 py-2 text-[0.78rem] font-mono rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] focus:outline-none focus:border-[var(--color-rule-strong)] resize-y"
              />
              {updatePromptMut.error && (
                <p className="text-[0.7rem] text-[var(--color-brick)]">
                  Save failed: {updatePromptMut.error.message}
                </p>
              )}
              <div className="flex items-center justify-between gap-2">
                <p className="text-[0.7rem] text-[var(--color-ink-4)]">
                  {promptDirty
                    ? 'Unsaved changes — saving writes the skill row immediately.'
                    : 'Edits persist to the skill row when saved.'}
                </p>
                <button
                  type="button"
                  disabled={!promptDirty || updatePromptMut.isPending}
                  onClick={() => {
                    if (promptDraft != null) {
                      updatePromptMut.mutate({ systemPrompt: promptDraft });
                    }
                  }}
                  className={[
                    'px-3 py-1 text-[0.78rem] rounded-[var(--radius-sm)] border transition-colors',
                    !promptDirty || updatePromptMut.isPending
                      ? 'border-[var(--color-rule-strong)] text-[var(--color-ink-4)] cursor-not-allowed'
                      : 'border-[var(--color-copper)] text-[var(--color-copper)] hover:bg-[var(--color-copper-soft)]',
                  ].join(' ')}
                >
                  {updatePromptMut.isPending ? 'Saving…' : 'Save prompt'}
                </button>
              </div>
            </>
          )}
        </div>
      </section>
    </div>
  );
}

function FeedbackGlyph() {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 13 13"
      aria-hidden
      fill="none"
      stroke="currentColor"
      strokeWidth="1.3"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M2 3.5 L2 8.5 A1 1 0 0 0 3 9.5 L4.5 9.5 L4.5 11.5 L7 9.5 L10 9.5 A1 1 0 0 0 11 8.5 L11 3.5 A1 1 0 0 0 10 2.5 L3 2.5 A1 1 0 0 0 2 3.5 Z" />
    </svg>
  );
}

function FeedbackHistory({
  rows,
  loading,
  decidePending,
  onDecide,
  decideError,
  autoExpandRefinementId,
}: {
  rows: EstimationFeedback[];
  loading: boolean;
  decidePending: boolean;
  onDecide: (refinementId: number, decision: 'apply' | 'dismiss') => void;
  decideError: ApiError | null;
  autoExpandRefinementId: number | null;
}) {
  if (loading) {
    return (
      <p className="mt-4 text-[0.78rem] text-[var(--color-ink-4)] italic">
        Loading feedback…
      </p>
    );
  }
  if (rows.length === 0) {
    return (
      <p className="mt-4 text-[0.78rem] text-[var(--color-ink-4)] italic">
        No feedback on this run yet.
      </p>
    );
  }
  return (
    <ul className="mt-4 space-y-3">
      {rows.map((row) => (
        <FeedbackRow
          key={row.id}
          row={row}
          decidePending={decidePending}
          onDecide={onDecide}
          decideError={decideError}
          defaultExpanded={
            autoExpandRefinementId != null &&
            row.refinement_id === autoExpandRefinementId
          }
        />
      ))}
    </ul>
  );
}

function FeedbackRow({
  row,
  decidePending,
  onDecide,
  decideError,
  defaultExpanded = false,
}: {
  row: EstimationFeedback;
  decidePending: boolean;
  onDecide: (refinementId: number, decision: 'apply' | 'dismiss') => void;
  decideError: ApiError | null;
  defaultExpanded?: boolean;
}) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const refinementQ = useQuery<SkillRefinement, ApiError>({
    queryKey: ['skill-refinement', row.refinement_id],
    queryFn: () =>
      fetchSkillRefinement(row.refinement_id as number),
    enabled: row.refinement_id != null && expanded,
    staleTime: 30_000,
  });

  return (
    <li className="px-4 py-3 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)]">
      <div className="flex items-baseline justify-between gap-3 flex-wrap">
        <FeedbackStatusBadge status={row.status} />
        <span
          className="text-[0.7rem] tracking-wide text-[var(--color-ink-4)]"
          title={fmtAbsolute(row.submitted_at)}
        >
          {fmtRelative(row.submitted_at)}
        </span>
      </div>
      <p className="mt-2 text-[0.85rem] text-[var(--color-ink-2)] leading-relaxed whitespace-pre-wrap">
        {row.feedback_text}
      </p>
      {row.refinement_id != null && (
        <div className="mt-3">
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            aria-expanded={expanded}
            className="text-[0.78rem] tracking-wide text-[var(--color-copper)] hover:text-[var(--color-copper-2)] underline-offset-2 hover:underline"
          >
            {expanded ? 'Hide proposal' : 'View proposed change'}
          </button>
          {expanded && (
            <RefinementProposal
              query={refinementQ}
              feedbackStatus={row.status}
              decidePending={decidePending}
              onDecide={onDecide}
              decideError={decideError}
            />
          )}
        </div>
      )}
    </li>
  );
}

function FeedbackStatusBadge({ status }: { status: FeedbackStatus }) {
  const tone =
    status === 'applied'
      ? 'bg-[var(--color-sage-soft)] text-[var(--color-sage)] border-[var(--color-sage)]/25'
      : status === 'failed'
        ? 'bg-[var(--color-brick-soft)] text-[var(--color-brick)] border-[var(--color-brick)]/25'
        : status === 'dismissed'
          ? 'bg-[var(--color-paper-2)] text-[var(--color-ink-3)] border-[var(--color-rule-strong)]'
          : status === 'proposed'
            ? 'bg-[var(--color-copper-soft)] text-[var(--color-copper)] border-[var(--color-copper)]/25'
            : 'bg-[var(--color-ochre-soft)] text-[var(--color-ochre)] border-[var(--color-ochre)]/25';
  return (
    <span
      className={[
        'inline-block px-2 py-0.5 text-[0.6rem] tracking-[0.16em] uppercase rounded-[var(--radius-xs)] border font-medium',
        tone,
      ].join(' ')}
    >
      {status}
    </span>
  );
}

function RefinementProposal({
  query,
  feedbackStatus,
  decidePending,
  onDecide,
  decideError,
}: {
  query: ReturnType<typeof useQuery<SkillRefinement, ApiError>>;
  feedbackStatus: FeedbackStatus;
  decidePending: boolean;
  onDecide: (refinementId: number, decision: 'apply' | 'dismiss') => void;
  decideError: ApiError | null;
}) {
  if (query.isLoading) {
    return (
      <p className="mt-2 text-[0.78rem] text-[var(--color-ink-4)] italic">
        Loading proposal…
      </p>
    );
  }
  if (query.error) {
    return (
      <p className="mt-2 text-[0.78rem] text-[var(--color-brick)]">
        Could not load proposal: {query.error.message}
      </p>
    );
  }
  const r = query.data;
  if (!r) return null;

  return (
    <div className="mt-3 space-y-3">
      <div>
        <p className="text-[0.6rem] tracking-[0.16em] uppercase text-[var(--color-ink-4)]">
          Refiner explanation
        </p>
        <p className="mt-1 text-[0.85rem] text-[var(--color-ink-2)] leading-relaxed italic">
          {r.refiner_explanation}
        </p>
      </div>
      <div>
        <p className="text-[0.6rem] tracking-[0.16em] uppercase text-[var(--color-ink-4)]">
          Skill: {r.skill_name} · status: {r.status}
        </p>
      </div>
      <PromptDiff before={r.original_prompt} after={r.proposed_prompt} />

      {r.status === 'proposed' && feedbackStatus !== 'applied' && (
        <div className="flex items-center gap-3 pt-1">
          <button
            type="button"
            disabled={decidePending}
            onClick={() => onDecide(r.id, 'apply')}
            className={[
              'px-4 py-2 text-sm rounded-[var(--radius-sm)] border transition-colors',
              decidePending
                ? 'bg-[var(--color-rule-strong)] text-[var(--color-ink-4)] border-[var(--color-rule-strong)] cursor-not-allowed'
                : 'bg-[var(--color-copper)] text-white border-[var(--color-copper)] hover:bg-[var(--color-copper-2)] hover:border-[var(--color-copper-2)]',
            ].join(' ')}
          >
            {decidePending ? 'Working…' : 'Apply to skill'}
          </button>
          <button
            type="button"
            disabled={decidePending}
            onClick={() => onDecide(r.id, 'dismiss')}
            className="px-3 py-2 text-sm rounded-[var(--radius-sm)] text-[var(--color-ink-3)] hover:text-[var(--color-brick)] transition-colors"
          >
            Dismiss
          </button>
        </div>
      )}
      {decideError && (
        <p className="text-[0.78rem] text-[var(--color-brick)]">
          Decision failed: {decideError.message}
        </p>
      )}
    </div>
  );
}

function PromptDiff({ before, after }: { before: string; after: string }) {
  const lines = useMemo(() => computeLineDiff(before, after), [before, after]);
  return (
    <div className="rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-inset)] overflow-x-auto max-h-[28rem] overflow-y-auto">
      <pre className="font-mono text-[0.72rem] leading-snug whitespace-pre">
        {lines.map((l, i) => (
          <span
            key={i}
            className={
              l.kind === 'add'
                ? 'block bg-[var(--color-sage-soft)] text-[var(--color-sage)] px-2'
                : l.kind === 'del'
                  ? 'block bg-[var(--color-brick-soft)] text-[var(--color-brick)] px-2'
                  : 'block text-[var(--color-ink-2)] px-2'
            }
          >
            {l.kind === 'add' ? '+ ' : l.kind === 'del' ? '- ' : '  '}
            {l.text}
          </span>
        ))}
      </pre>
    </div>
  );
}

/**
 * Cheap line-based diff. Not LCS — we just emit deletions of lines
 * absent from `after` and additions of lines absent from `before`,
 * preserving order from the original. Good enough for prompt diffs
 * where the operator mostly wants to see what was inserted /
 * removed, not full unified-diff hunks.
 */
function computeLineDiff(
  before: string,
  after: string,
): Array<{ kind: 'add' | 'del' | 'ctx'; text: string }> {
  const beforeLines = before.split('\n');
  const afterLines = after.split('\n');
  const beforeSet = new Set(beforeLines);
  const afterSet = new Set(afterLines);
  const out: Array<{ kind: 'add' | 'del' | 'ctx'; text: string }> = [];
  let i = 0;
  let j = 0;
  while (i < beforeLines.length || j < afterLines.length) {
    const a = i < beforeLines.length ? beforeLines[i] : null;
    const b = j < afterLines.length ? afterLines[j] : null;
    if (a === b) {
      out.push({ kind: 'ctx', text: a ?? '' });
      i++;
      j++;
      continue;
    }
    if (a != null && !afterSet.has(a)) {
      out.push({ kind: 'del', text: a });
      i++;
      continue;
    }
    if (b != null && !beforeSet.has(b)) {
      out.push({ kind: 'add', text: b });
      j++;
      continue;
    }
    if (a != null) {
      out.push({ kind: 'ctx', text: a });
      i++;
    }
    if (b != null) {
      out.push({ kind: 'ctx', text: b });
      j++;
    }
  }
  return out;
}

async function fetchSkillRefinement(id: number): Promise<SkillRefinement> {
  const { apiGet } = await import('@/lib/api');
  return apiGet<SkillRefinement>(`/skill-refinements/${id}`);
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
