import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import Tabs, { type Tab } from './Tabs';
import {
  fetchEstimationPreview,
  fetchListingById,
} from '@/lib/queries';
import { ApiError } from '@/lib/api';
import type {
  ListingPublic,
  PreviewListing,
  PreviewResponse,
  TargetSpecIn,
} from '@/lib/types';

export interface ResolvedInput {
  spec: TargetSpecIn;
  listing: PreviewListing;
  origin:
    | { kind: 'url'; url: string; sreality_id: number; in_database: boolean; fetched_at: string }
    | { kind: 'listing'; sreality_id: number };
}

interface Props {
  onResolved: (input: ResolvedInput) => void;
}

type TabKey = 'url' | 'listing';

const TABS: ReadonlyArray<Tab<TabKey>> = [
  { key: 'url', label: 'Paste URL' },
  { key: 'listing', label: 'From a listing' },
];

export default function UrlScrapeStep({ onResolved }: Props) {
  const [tab, setTab] = useState<TabKey>('url');

  return (
    <div className="px-6 py-12 max-w-xl mx-auto">
      <header className="mb-6">
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          New estimate · step 1 of 2
        </p>
        <h1
          className="mt-2 text-[1.9rem] leading-tight"
          style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
        >
          Where is the listing?
        </h1>
        <p className="mt-2 text-sm text-[var(--color-ink-2)]">
          Paste a sreality.cz URL or pick a listing already in the database.
          You'll review and edit the specs before the estimate runs.
        </p>
      </header>

      <Tabs tabs={TABS} active={tab} onChange={setTab} />

      <div className="mt-6">
        {tab === 'url' ? (
          <UrlPanel onResolved={onResolved} />
        ) : (
          <ListingPanel onResolved={onResolved} />
        )}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Tab 1 — paste URL                                                          */
/* -------------------------------------------------------------------------- */

function UrlPanel({ onResolved }: { onResolved: (i: ResolvedInput) => void }) {
  const [url, setUrl] = useState('');

  const mut = useMutation<PreviewResponse, ApiError, string>({
    mutationFn: fetchEstimationPreview,
    onSuccess: (preview) => {
      onResolved({
        spec: preview.spec,
        listing: preview.listing,
        origin: {
          kind: 'url',
          url: preview.url,
          sreality_id: preview.sreality_id,
          in_database: preview.in_database,
          fetched_at: preview.fetched_at,
        },
      });
    },
  });

  const submit = () => {
    const trimmed = url.trim();
    if (!trimmed || mut.isPending) return;
    mut.mutate(trimmed);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div>
      <label
        htmlFor="estimate-url"
        className="block text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]"
      >
        sreality URL
      </label>
      <div className="mt-2 flex items-stretch gap-2">
        <input
          id="estimate-url"
          type="url"
          inputMode="url"
          autoFocus
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="https://www.sreality.cz/detail/pronajem/byt/..."
          disabled={mut.isPending}
          className="flex-1 min-w-0 px-3 py-2 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)] disabled:opacity-60"
        />
        <PrimaryButton
          onClick={submit}
          disabled={!url.trim() || mut.isPending}
          loading={mut.isPending}
        >
          Scrape
        </PrimaryButton>
      </div>

      {mut.error && <ErrorBlock error={mut.error} />}

      <p className="mt-4 text-[0.75rem] text-[var(--color-ink-3)] leading-relaxed">
        We fetch the listing from sreality.cz and parse it. Nothing is saved
        until you confirm the estimate on the next step.
      </p>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Tab 2 — pick by sreality_id                                                */
/* -------------------------------------------------------------------------- */

function ListingPanel({ onResolved }: { onResolved: (i: ResolvedInput) => void }) {
  const [raw, setRaw] = useState('');
  const [error, setError] = useState<string | null>(null);

  const mut = useMutation<ListingPublic | null, Error, number>({
    mutationFn: fetchListingById,
    onSuccess: (listing, sid) => {
      if (!listing) {
        setError(`No listing in our database with id ${sid}.`);
        return;
      }
      const resolved = listingToResolved(listing);
      if (resolved == null) {
        setError(
          'That listing is missing coordinates — pick another one or paste its URL.',
        );
        return;
      }
      setError(null);
      onResolved(resolved);
    },
    onError: (err) => {
      setError(err.message || 'Failed to look up the listing.');
    },
  });

  const submit = () => {
    setError(null);
    const cleaned = raw.replace(/\D+/g, '');
    if (cleaned.length < 6) {
      setError('Enter a sreality listing id (numeric, 6+ digits).');
      return;
    }
    mut.mutate(Number(cleaned));
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div>
      <label
        htmlFor="estimate-sid"
        className="block text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]"
      >
        sreality id
      </label>
      <div className="mt-2 flex items-stretch gap-2">
        <input
          id="estimate-sid"
          type="text"
          inputMode="numeric"
          autoFocus
          value={raw}
          onChange={(e) => setRaw(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="2836292428"
          disabled={mut.isPending}
          className="flex-1 min-w-0 px-3 py-2 text-sm font-mono tabular-nums rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)] disabled:opacity-60"
        />
        <PrimaryButton
          onClick={submit}
          disabled={!raw.trim() || mut.isPending}
          loading={mut.isPending}
        >
          Lookup
        </PrimaryButton>
      </div>

      {error && <InlineError>{error}</InlineError>}

      <p className="mt-4 text-[0.75rem] text-[var(--color-ink-3)] leading-relaxed">
        Looks up the listing in our database — same data the Browse and
        Listing pages use. No fetch from sreality.
      </p>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Shared bits                                                                */
/* -------------------------------------------------------------------------- */

function PrimaryButton({
  onClick,
  disabled,
  loading,
  children,
}: {
  onClick: () => void;
  disabled: boolean;
  loading: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={[
        'shrink-0 px-4 py-2 text-sm rounded-[var(--radius-sm)] border transition-colors',
        disabled
          ? 'bg-[var(--color-rule-strong)] text-[var(--color-ink-4)] border-[var(--color-rule-strong)] cursor-not-allowed'
          : 'bg-[var(--color-copper)] text-white border-[var(--color-copper)] hover:bg-[var(--color-copper-2)] hover:border-[var(--color-copper-2)]',
      ].join(' ')}
    >
      {loading ? <Spinner /> : children}
    </button>
  );
}

function Spinner() {
  return (
    <span className="inline-flex items-center gap-2">
      <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden>
        <circle
          cx="6" cy="6" r="4.5"
          stroke="currentColor" strokeWidth="1.5"
          strokeOpacity="0.25" fill="none"
        />
        <path
          d="M6 1.5 a 4.5 4.5 0 0 1 4.5 4.5"
          stroke="currentColor" strokeWidth="1.5" fill="none"
          strokeLinecap="round"
        >
          <animateTransform
            attributeName="transform" type="rotate"
            from="0 6 6" to="360 6 6" dur="0.9s"
            repeatCount="indefinite"
          />
        </path>
      </svg>
      <span>Working…</span>
    </span>
  );
}

function ErrorBlock({ error }: { error: ApiError }) {
  const headline = (() => {
    if (error.status === 400) {
      return "That URL doesn't look like a sreality.cz detail page.";
    }
    if (error.status === 401) {
      return 'API authentication failed. Check VITE_API_TOKEN.';
    }
    if (error.status === 502) {
      return "Couldn't reach sreality.cz right now.";
    }
    if (error.status === 0) {
      return 'Network error — check your connection or the API URL.';
    }
    return `Request failed (HTTP ${error.status}).`;
  })();
  return (
    <div className="mt-3 px-3 py-2 rounded-[var(--radius-sm)] border border-[var(--color-brick)]/30 bg-[var(--color-brick-soft)] text-[var(--color-brick)] text-sm">
      <p className="font-medium">{headline}</p>
      {error.message && error.message !== headline && (
        <p className="mt-1 text-[0.78rem] text-[var(--color-brick)]/85">
          {error.message}
        </p>
      )}
    </div>
  );
}

function InlineError({ children }: { children: React.ReactNode }) {
  return (
    <p className="mt-3 text-sm text-[var(--color-brick)]">{children}</p>
  );
}

/* -------------------------------------------------------------------------- */
/* Listing → ResolvedInput shape conversion                                   */
/* -------------------------------------------------------------------------- */

export function listingToResolved(listing: ListingPublic): ResolvedInput | null {
  if (listing.lat == null || listing.lng == null) return null;
  const spec: TargetSpecIn = {
    lat: listing.lat,
    lng: listing.lng,
    area_m2: listing.area_m2,
    disposition: listing.disposition,
    floor: listing.floor,
    exclude_ids: [listing.sreality_id],
  };
  const previewListing: PreviewListing = {
    price_czk: listing.price_czk,
    price_unit: listing.price_unit,
    category_main:
      listing.category_main != null ? String(listing.category_main) : null,
    category_type:
      listing.category_type != null ? String(listing.category_type) : null,
    locality: listing.locality,
    district: listing.district,
    locality_district_id: listing.locality_district_id,
    locality_region_id: listing.locality_region_id,
    total_floors: listing.total_floors,
    has_balcony: listing.has_balcony,
    has_lift: listing.has_lift,
    has_parking: listing.has_parking,
    building_type: listing.building_type,
    condition: listing.condition,
    energy_rating: listing.energy_rating,
    image_count: 0,
  };
  return {
    spec,
    listing: previewListing,
    origin: { kind: 'listing', sreality_id: listing.sreality_id },
  };
}
