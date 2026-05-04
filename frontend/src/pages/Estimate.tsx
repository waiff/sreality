import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import UrlScrapeStep, {
  type ResolvedInput,
  listingToResolved,
} from '@/components/UrlScrapeStep';
import { fetchListingById } from '@/lib/queries';
import {
  fmtArea,
  fmtCzk,
} from '@/lib/format';

type Stage =
  | { kind: 'input' }
  | { kind: 'editing'; resolved: ResolvedInput };

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
    setStage({ kind: 'editing', resolved });
    const next = new URLSearchParams(params);
    next.delete('from_listing');
    setParams(next, { replace: true });
  }, [fromListingId, fromListingQuery.data, stage.kind, params, setParams]);

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
    return (
      <UrlScrapeStep
        onResolved={(resolved) => setStage({ kind: 'editing', resolved })}
      />
    );
  }

  return (
    <Step2Placeholder
      resolved={stage.resolved}
      onBack={() => setStage({ kind: 'input' })}
    />
  );
}

/* -------------------------------------------------------------------------- */
/* Step 2 placeholder — the real editable form lands in the next checkpoint.  */
/* For now this lets the user click through and verify scraping works.        */
/* -------------------------------------------------------------------------- */

function Step2Placeholder({
  resolved,
  onBack,
}: {
  resolved: ResolvedInput;
  onBack: () => void;
}) {
  const { spec, listing, origin } = resolved;
  const summary =
    [
      spec.disposition,
      spec.area_m2 != null ? fmtArea(spec.area_m2) : null,
      listing.district,
    ]
      .filter(Boolean)
      .join(' · ') || '—';

  return (
    <div className="px-6 py-12 max-w-3xl mx-auto">
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
        <p className="mt-2 text-sm text-[var(--color-ink-2)]">
          {summary}
          {listing.price_czk != null && (
            <>
              <span className="mx-2 text-[var(--color-ink-4)]">·</span>
              <span className="font-mono tabular-nums">{fmtCzk(listing.price_czk)}</span>
            </>
          )}
        </p>
      </header>

      <div className="my-7 h-px bg-[var(--color-rule)]" />

      <SourceLine origin={origin} />

      <div className="my-7 h-px bg-[var(--color-rule)]" />

      <div>
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
          Parsed spec (preview)
        </p>
        <pre className="mt-3 px-3 py-3 text-[0.75rem] font-mono leading-relaxed bg-[var(--color-inset)] border border-[var(--color-rule)] rounded-[var(--radius-md)] overflow-x-auto text-[var(--color-ink-2)]">
{JSON.stringify({ spec, listing }, null, 2)}
        </pre>
      </div>

      <p className="mt-6 text-[0.78rem] text-[var(--color-ink-3)]">
        The editable specs form and the Estimate button arrive in the next
        step. For now: confirm step 1 captured the right data.
      </p>
    </div>
  );
}

function SourceLine({ origin }: { origin: ResolvedInput['origin'] }) {
  if (origin.kind === 'url') {
    return (
      <div className="flex items-baseline gap-3 flex-wrap">
        <span className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          Source
        </span>
        <a
          href={origin.url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-sm text-[var(--color-copper)] hover:text-[var(--color-copper-2)] underline-offset-2 hover:underline truncate max-w-full"
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
      </div>
    );
  }
  return (
    <div className="flex items-baseline gap-3">
      <span className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        Source
      </span>
      <span className="text-sm text-[var(--color-ink-2)]">
        Picked from database — id{' '}
        <span className="font-mono tabular-nums">{origin.sreality_id}</span>
      </span>
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
