/* Compact popup for kicking off a new estimation.
 *
 * Operator pastes a listing URL → we call POST /estimations/preview to
 * extract the spec → we POST /estimations immediately with the scraped
 * spec (no review) → navigate to /estimation/:id. Any fine-tuning of
 * the spec or filters happens on the detail page's "Adjust & re-run"
 * panel. The pop-up therefore only carries the minimum a sane default
 * estimation needs.
 *
 * Estimate kind (rent vs sale) is auto-detected from the preview's
 * category_type (pronájem / prodej) so the popup itself stays
 * single-field. Provider and population fall back to the same
 * defaults the form used to ship with (Claude + active-only).
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation } from '@tanstack/react-query';
import { ApiError, previewListingUrl } from '@/lib/api';
import { submitEstimation } from '@/lib/queries';
import type {
  CreateEstimationIn,
  EstimationRun,
  ParseResult,
} from '@/lib/types';

/* -------------------------------------------------------------------------- */
/* Context — wires the CTA button (Shell) and the "+ New estimation"          */
/* affordances on EstimationList / empty states to the same modal instance.   */
/* -------------------------------------------------------------------------- */

interface ModalCtx {
  open: () => void;
  close: () => void;
  isOpen: boolean;
}

const ctx = createContext<ModalCtx | null>(null);

export function useNewEstimationModal(): ModalCtx {
  const v = useContext(ctx);
  if (!v) {
    throw new Error('useNewEstimationModal must be used inside <NewEstimationProvider>');
  }
  return v;
}

export function NewEstimationProvider({ children }: { children: ReactNode }) {
  const [isOpen, setOpen] = useState(false);
  const value = useMemo<ModalCtx>(
    () => ({
      open: () => setOpen(true),
      close: () => setOpen(false),
      isOpen,
    }),
    [isOpen],
  );
  return (
    <ctx.Provider value={value}>
      {children}
      {isOpen && <NewEstimationModal onClose={() => setOpen(false)} />}
    </ctx.Provider>
  );
}

/* -------------------------------------------------------------------------- */
/* Modal                                                                      */
/* -------------------------------------------------------------------------- */

function NewEstimationModal({ onClose }: { onClose: () => void }) {
  const [url, setUrl] = useState('');
  const navigate = useNavigate();

  const previewMut = useMutation<ParseResult, ApiError, string>({
    mutationFn: (url) => previewListingUrl(url),
  });
  const submitMut = useMutation<EstimationRun, ApiError, ParseResult>({
    mutationFn: (preview) => submitEstimation(buildPayload(preview)),
    onSuccess: (run) => {
      onClose();
      navigate(`/estimation/${run.id}`);
    },
  });

  const pending = previewMut.isPending || submitMut.isPending;
  const error = previewMut.error || submitMut.error;

  const submit = useCallback(() => {
    const trimmed = url.trim();
    if (!trimmed || pending) return;
    previewMut.mutate(trimmed, {
      onSuccess: (preview) => submitMut.mutate(preview),
    });
  }, [url, pending, previewMut, submitMut]);

  // Esc closes; Enter submits when the input is focused.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (pending) return;
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose, pending]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center px-4 pt-[16vh] pb-10 bg-[var(--color-ink)]/40 backdrop-blur-[2px]"
      role="dialog"
      aria-modal="true"
      aria-labelledby="new-estimation-title"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !pending) onClose();
      }}
    >
      <div className="w-full max-w-xl rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper)] shadow-2xl">
        <header className="flex items-baseline justify-between gap-4 px-6 pt-5 pb-3">
          <div>
            <p className="text-[0.65rem] tracking-[0.22em] uppercase text-[var(--color-ink-3)]">
              New estimation
            </p>
            <h2
              id="new-estimation-title"
              className="mt-1 text-[1.35rem] leading-tight"
              style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
            >
              Where is the listing?
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={pending}
            aria-label="Close"
            className="shrink-0 -mr-1 px-2 py-1 text-[var(--color-ink-3)] hover:text-[var(--color-ink)] disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <CloseGlyph />
          </button>
        </header>

        <div className="px-6 pb-6">
          <label
            htmlFor="new-estimation-url"
            className="block text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]"
          >
            Listing URL
          </label>
          <div className="mt-2 flex items-stretch gap-2">
            <input
              id="new-estimation-url"
              type="url"
              inputMode="url"
              autoFocus
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  submit();
                }
              }}
              placeholder="https://www.sreality.cz/detail/…"
              disabled={pending}
              className="flex-1 min-w-0 px-3 py-2 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)] disabled:opacity-60"
            />
            <button
              type="button"
              onClick={submit}
              disabled={!url.trim() || pending}
              className={[
                'shrink-0 px-4 py-2 text-sm rounded-[var(--radius-sm)] border transition-colors',
                !url.trim() || pending
                  ? 'bg-[var(--color-rule-strong)] text-[var(--color-ink-4)] border-[var(--color-rule-strong)] cursor-not-allowed'
                  : 'bg-[var(--color-copper)] text-white border-[var(--color-copper)] hover:bg-[var(--color-copper-2)] hover:border-[var(--color-copper-2)]',
              ].join(' ')}
            >
              {pending ? <Spinner label={previewMut.isPending ? 'Scraping…' : 'Submitting…'} /> : 'Estimate'}
            </button>
          </div>

          {error && <ErrorBlock error={error} />}

          <p className="mt-4 text-[0.75rem] text-[var(--color-ink-3)] leading-relaxed">
            We scrape the listing, kick off the estimate, and open the run.
            You can adjust the spec and re-run from the detail page.
          </p>
        </div>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Helpers                                                                    */
/* -------------------------------------------------------------------------- */

function buildPayload(preview: ParseResult): CreateEstimationIn {
  const kind = previewToEstimateKind(preview);
  const mode = kind === 'rent' ? 'agent' : 'deterministic';
  return {
    source: 'ui',
    mode,
    provider: 'anthropic',
    population: 'active',
    estimate_kind: kind,
    url: preview.source_url,
  };
}

function previewToEstimateKind(preview: ParseResult): 'rent' | 'sale' {
  const ct = preview.listing.category_type?.toLowerCase() ?? null;
  if (ct === 'prodej' || ct === 'sale') return 'sale';
  return 'rent';
}

function Spinner({ label }: { label: string }) {
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
      <span>{label}</span>
    </span>
  );
}

function CloseGlyph() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden>
      <path
        d="M3 3 L11 11 M11 3 L3 11"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

function ErrorBlock({ error }: { error: ApiError }) {
  const headline = (() => {
    if (error.status === 400) {
      return "That URL doesn't look like a supported listing page.";
    }
    if (error.status === 401) {
      return 'API authentication failed. Check VITE_API_TOKEN.';
    }
    if (error.status === 502) {
      return "Couldn't reach the listing source right now.";
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
