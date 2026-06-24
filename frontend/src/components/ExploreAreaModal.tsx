/* "Explore area" modal — opens the full Browse experience focused on a single
 * property's neighbourhood (~5 km viewport) and pre-filtered to its category +
 * disposition. Reuses <BrowseExperience> via the in-memory state adapter, so
 * the map, filters, tabs and every overlay are identical to the Browse page.
 *
 * App-wide-triggerable via the same provider+hook pattern as NewEstimationModal
 * (mounted once in Shell), so future surfaces (Region, Collections) can reuse
 * it. The "Go to Browse" link serializes the modal's current view-state to a
 * /browse URL so whatever the operator explored carries over to the full page.
 */
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { useNavigate } from 'react-router-dom';
import BrowseExperience from '@/components/BrowseExperience';
import OriginPropertyPanel from '@/components/OriginPropertyPanel';
import type { AnchorPoint } from '@/components/ListingMap';
import {
  browseFiltersForArea,
  browseUrlFromState,
  useMemoryBrowseState,
  type ExploreAreaSeed,
} from '@/lib/browseState';

export type ExploreAreaPayload = ExploreAreaSeed & {
  /* Human label for the modal header (e.g. "Třebíč - Borovina · 2+1"). */
  label?: string;
};

interface ModalCtx {
  open: (payload: ExploreAreaPayload) => void;
  close: () => void;
  isOpen: boolean;
}

const ctx = createContext<ModalCtx | null>(null);

export function useExploreAreaModal(): ModalCtx {
  const v = useContext(ctx);
  if (!v) {
    throw new Error('useExploreAreaModal must be used inside <ExploreAreaProvider>');
  }
  return v;
}

export function ExploreAreaProvider({ children }: { children: ReactNode }) {
  const [payload, setPayload] = useState<ExploreAreaPayload | null>(null);
  const value = useMemo<ModalCtx>(
    () => ({
      open: (p) => setPayload(p),
      close: () => setPayload(null),
      isOpen: payload != null,
    }),
    [payload],
  );
  return (
    <ctx.Provider value={value}>
      {children}
      {payload && (
        <ExploreAreaModal payload={payload} onClose={() => setPayload(null)} />
      )}
    </ctx.Provider>
  );
}

function ExploreAreaModal({
  payload,
  onClose,
}: {
  payload: ExploreAreaPayload;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const initialFilters = useMemo(() => browseFiltersForArea(payload), [payload]);
  const view = useMemoryBrowseState({ filters: initialFilters });

  /* The property the operator came FROM — pinned on the map (anchor) and shown
   * in the top panel, both independent of the filter cohort. Memoized so the
   * anchor object identity is stable across filter-driven re-renders (else the
   * map's anchor setData effect would fire on every keystroke). Uses the seed's
   * guaranteed numeric coords (the same the trigger button passed). */
  const origin = payload.origin ?? null;
  const anchor = useMemo<AnchorPoint | null>(
    () =>
      origin
        ? {
            lat: payload.lat,
            lng: payload.lng,
            is_active: origin.listing.is_active,
            sreality_id: origin.listing.sreality_id,
          }
        : null,
    [origin, payload.lat, payload.lng],
  );

  // ESC closes + lock body scroll while the (tall) modal is open.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      window.removeEventListener('keydown', onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose]);

  const browseHref = browseUrlFromState({
    filters: view.filters,
    sort: view.sort,
    tab: view.tab,
    overlay: view.overlay,
  });

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-3 sm:p-4 bg-[var(--color-ink)]/40 backdrop-blur-[2px]"
      role="dialog"
      aria-modal="true"
      aria-label="Explore area"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="w-[96vw] max-w-[1600px] h-[90vh] flex flex-col rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper)] shadow-2xl overflow-hidden">
        <header className="flex items-center justify-between gap-4 px-6 py-3 border-b border-[var(--color-rule)] shrink-0">
          <div className="min-w-0">
            <p className="text-[0.62rem] tracking-[0.22em] uppercase text-[var(--color-ink-3)]">
              Explore area
            </p>
            <h2
              className="mt-0.5 text-[1.1rem] leading-tight truncate"
              style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
            >
              {payload.label ?? 'Okolí nemovitosti'}
            </h2>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <a
              href={browseHref}
              onClick={(e) => {
                // Let modifier / middle clicks open a new tab; otherwise
                // SPA-navigate and close the modal.
                if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return;
                e.preventDefault();
                navigate(browseHref);
                onClose();
              }}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)] transition-colors"
              title="Open the full Browse page in this same area + filters"
            >
              <span>Go to Browse</span>
              <span aria-hidden>→</span>
            </a>
            <button
              type="button"
              onClick={onClose}
              aria-label="Close"
              className="shrink-0 px-2 py-1 text-[var(--color-ink-3)] hover:text-[var(--color-ink)] transition-colors"
            >
              <CloseGlyph />
            </button>
          </div>
        </header>
        {origin && (
          <OriginPropertyPanel listing={origin.listing} images={origin.images} />
        )}
        <div className="flex-1 min-h-0">
          <BrowseExperience
            view={view}
            layout="modal"
            anchor={anchor}
            features={{ presetBar: false, mergeMode: false, watchdog: false, title: false }}
          />
        </div>
      </div>
    </div>
  );
}

function CloseGlyph() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
      <line x1="3.5" y1="3.5" x2="12.5" y2="12.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      <line x1="12.5" y1="3.5" x2="3.5" y2="12.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}
