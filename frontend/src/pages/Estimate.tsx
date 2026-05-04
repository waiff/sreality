import { Suspense, lazy, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import UrlScrapeStep, {
  type ResolvedInput,
  listingToResolved,
} from '@/components/UrlScrapeStep';
import EstimateForm, {
  type EstimateFormState,
  buildInitialFormState,
} from '@/components/EstimateForm';
import { fetchListingById } from '@/lib/queries';
import {
  fmtArea,
  fmtCzk,
  fmtPricePerM2,
} from '@/lib/format';
import type { PreviewListing } from '@/lib/types';

const RegionMap = lazy(() => import('@/components/region/RegionMap'));

type Stage =
  | { kind: 'input' }
  | { kind: 'editing'; resolved: ResolvedInput; form: EstimateFormState };

export default function Estimate() {
  const [params, setParams] = useSearchParams();
  const fromListingRaw = params.get('from_listing');
  const fromListingId =
    fromListingRaw && /^\d+$/.test(fromListingRaw)
      ? Number(fromListingRaw)
      : null;

  const [stage, setStage] = useState<Stage>({ kind: 'input' });

  const fromListingQuery = useQuery({
    queryKey: ['estimate-from-listing', fromListingId],
    queryFn: () => fetchListingById(fromListingId as number),
    enabled: fromListingId != null && stage.kind === 'input',
    staleTime: 60_000,
  });

  useEffect(() => {
    if (stage.kind !== 'input') return;
    if (fromListingId == null) return;
    const listing = fromListingQuery.data;
    if (!listing) return;
    const resolved = listingToResolved(listing);
    if (resolved == null) return;
    enterEditing(resolved);
    const next = new URLSearchParams(params);
    next.delete('from_listing');
    setParams(next, { replace: true });
  }, [fromListingId, fromListingQuery.data, stage.kind, params, setParams]);

  const enterEditing = (resolved: ResolvedInput) => {
    setStage({
      kind: 'editing',
      resolved,
      form: buildInitialFormState(resolved.spec, resolved.listing),
    });
  };

  if (stage.kind === 'input') {
    if (fromListingId != null && fromListingQuery.isLoading) {
      return <FromListingLoading id={fromListingId} />;
    }
    if (fromListingId != null && fromListingQuery.isError) {
      return (
        <FromListingError
          id={fromListingId}
          message={(fromListingQuery.error as Error)?.message ?? 'Failed to load listing.'}
        />
      );
    }
    if (
      fromListingId != null &&
      !fromListingQuery.isLoading &&
      fromListingQuery.data === null
    ) {
      return (
        <FromListingError
          id={fromListingId}
          message="Listing not found in our database."
        />
      );
    }
    return <UrlScrapeStep onResolved={enterEditing} />;
  }

  return (
    <EditingStage
      resolved={stage.resolved}
      form={stage.form}
      onForm={(form) => setStage({ ...stage, form })}
      onBack={() => setStage({ kind: 'input' })}
    />
  );
}

/* -------------------------------------------------------------------------- */
/* Editing stage — two-column layout: form (left) + live target preview.      */
/* -------------------------------------------------------------------------- */

function EditingStage({
  resolved,
  form,
  onForm,
  onBack,
}: {
  resolved: ResolvedInput;
  form: EstimateFormState;
  onForm: (next: EstimateFormState) => void;
  onBack: () => void;
}) {
  return (
    <div className="px-6 py-8 max-w-6xl mx-auto">
      <button
        type="button"
        onClick={onBack}
        className="inline-flex items-center gap-1.5 text-[0.75rem] tracking-wide text-[var(--color-ink-3)] hover:text-[var(--color-copper)] transition-colors"
      >
        <BackArrow />
        <span>Back to step 1</span>
      </button>

      <header className="mt-5">
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          New estimate · step 2 of 2
        </p>
        <h1
          className="mt-2 text-[1.9rem] leading-tight"
          style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
        >
          Review specs
        </h1>
        <SourceLine origin={resolved.origin} />
      </header>

      <div className="my-6 h-px bg-[var(--color-rule)]" />

      <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,_1fr)_360px] gap-10">
        <EstimateForm
          state={form}
          onChange={onForm}
          onSubmit={() => {
            // Wired up in the next step (Part B submit + redirect).
            // eslint-disable-next-line no-console
            console.log('CreateEstimationIn payload (preview):', form);
          }}
          submitting={false}
        />
        <TargetPreview
          form={form}
          listing={resolved.listing}
          onMapChange={({ lat, lng }) => onForm({ ...form, lat, lng })}
        />
      </div>
    </div>
  );
}

function SourceLine({ origin }: { origin: ResolvedInput['origin'] }) {
  if (origin.kind === 'url') {
    return (
      <p className="mt-2 text-sm text-[var(--color-ink-2)] flex items-baseline gap-2 flex-wrap">
        <span>From</span>
        <a
          href={origin.url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-[var(--color-copper)] hover:text-[var(--color-copper-2)] underline-offset-2 hover:underline truncate max-w-[60ch]"
        >
          {origin.url}
        </a>
        <span className="font-mono tabular-nums text-[var(--color-ink-3)] text-[0.78rem]">
          id {origin.sreality_id}
        </span>
        {origin.in_database && (
          <span className="px-2 py-0.5 text-[0.65rem] tracking-[0.14em] uppercase rounded-[var(--radius-xs)] bg-[var(--color-copper-soft)] text-[var(--color-copper)]">
            already scraped
          </span>
        )}
      </p>
    );
  }
  return (
    <p className="mt-2 text-sm text-[var(--color-ink-2)]">
      Picked from database — id{' '}
      <span className="font-mono tabular-nums">{origin.sreality_id}</span>
    </p>
  );
}

/* -------------------------------------------------------------------------- */
/* Right column: live preview (map + key facts)                               */
/* -------------------------------------------------------------------------- */

function TargetPreview({
  form,
  listing,
  onMapChange,
}: {
  form: EstimateFormState;
  listing: PreviewListing;
  onMapChange: (next: { lat: number; lng: number }) => void;
}) {
  const center = useMemo(() => {
    if (form.lat == null || form.lng == null) return null;
    return { lat: form.lat, lng: form.lng };
  }, [form.lat, form.lng]);

  return (
    <aside className="space-y-5 lg:sticky lg:top-20 self-start">
      <div>
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
          Target
        </p>
        <div className="mt-2">
          {center ? (
            <Suspense
              fallback={
                <div className="h-[280px] rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)]" />
              }
            >
              <RegionMap
                center={center}
                radiusM={form.radius_m}
                onCenterChange={onMapChange}
              />
            </Suspense>
          ) : (
            <div className="h-[280px] rounded-[var(--radius-md)] border border-dashed border-[var(--color-rule)] flex items-center justify-center text-sm text-[var(--color-ink-3)]">
              No coordinates set
            </div>
          )}
        </div>
        <p className="mt-2 text-[0.7rem] text-[var(--color-ink-4)] leading-relaxed">
          Copper circle = comparables search radius
          ({form.radius_m.toLocaleString('cs-CZ')} m).
        </p>
      </div>

      <div>
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
          Listing facts
        </p>
        <dl className="mt-3 grid grid-cols-2 gap-x-5 gap-y-3">
          <KV label="Asking">
            <span className="font-mono tabular-nums">
              {fmtCzk(listing.price_czk)}
            </span>
          </KV>
          <KV label="Per m²">
            <span className="font-mono tabular-nums">
              {fmtPricePerM2(listing.price_czk, form.area_m2)}
            </span>
          </KV>
          <KV label="Disposition">
            <span className="font-mono tabular-nums">
              {form.disposition ?? '—'}
            </span>
          </KV>
          <KV label="Area">
            <span className="font-mono tabular-nums">
              {fmtArea(form.area_m2)}
            </span>
          </KV>
          <KV label="District" colSpan={2}>
            {listing.district ?? '—'}
          </KV>
          {listing.locality && listing.locality !== listing.district && (
            <KV label="Locality" colSpan={2}>
              <span className="text-[var(--color-ink-2)] text-[0.85rem]">
                {listing.locality}
              </span>
            </KV>
          )}
        </dl>
      </div>
    </aside>
  );
}

function KV({
  label, children, colSpan = 1,
}: {
  label: string;
  children: React.ReactNode;
  colSpan?: 1 | 2;
}) {
  return (
    <div className={colSpan === 2 ? 'col-span-2' : undefined}>
      <dt className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
        {label}
      </dt>
      <dd className="mt-0.5 text-sm text-[var(--color-ink)]">{children}</dd>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* from_listing query-param landing states                                    */
/* -------------------------------------------------------------------------- */

function FromListingLoading({ id }: { id: number }) {
  return (
    <div className="px-6 py-16 max-w-md mx-auto text-center">
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        Loading listing
      </p>
      <p className="mt-2 text-sm font-mono tabular-nums text-[var(--color-ink-2)]">
        id {id}
      </p>
    </div>
  );
}

function FromListingError({ id, message }: { id: number; message: string }) {
  return (
    <div className="px-6 py-16 max-w-md mx-auto text-center">
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        Couldn't load listing
      </p>
      <h1
        className="mt-2 text-2xl"
        style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
      >
        id <span className="font-mono tabular-nums text-[var(--color-ink-2)]">{id}</span>
      </h1>
      <p className="mt-3 text-sm text-[var(--color-brick)]">{message}</p>
      <a
        href="/estimate"
        className="mt-6 inline-block text-sm text-[var(--color-copper)] hover:text-[var(--color-copper-2)] underline-offset-2 hover:underline"
      >
        Start a new estimate
      </a>
    </div>
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
