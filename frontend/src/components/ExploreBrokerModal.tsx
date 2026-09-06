/* "Explore this broker's listings" modal — opens the full Browse experience
 * scoped to one broker's cohort (the brokerId prefilter, lib/queries.ts
 * resolveBrokerPrefilter), with the filter sidebar and Stats tab hidden: this
 * is a read-only, fully-scoped view, not an interactive filtering surface —
 * the operator's Typ/Nabídka choice on the Broker Detail page is what set the
 * scope, seeded in via `browseFiltersForBroker`.
 *
 * A sibling to ExploreAreaModal (frontend/src/components/ExploreAreaModal.tsx),
 * not a rewrite of it: that component is tightly shaped around "explore the
 * neighbourhood of one property" (an anchor pin, an origin-property panel, a
 * ~5 km viewport seed), and its own header comment already invites new
 * sibling surfaces to reuse the SAME PATTERN — mounting <BrowseExperience> via
 * the in-memory state adapter — rather than folding every seed shape into one
 * component. This one has no anchor and no viewport constraint: ListingMap
 * already auto-fits to the first non-empty result set, so the map frames
 * itself around wherever the broker's listings actually are.
 */
import { createContext, useContext, useMemo, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import Dialog, { DialogClose } from '@/components/Dialog';
import { useCloseOnNavigation } from '@/lib/useCloseOnNavigation';
import BrowseExperience from '@/components/BrowseExperience';
import {
  browseFiltersForBroker,
  browseUrlFromState,
  useMemoryBrowseState,
  type ExploreBrokerSeed,
} from '@/lib/browseState';

export type ExploreBrokerPayload = ExploreBrokerSeed & {
  /* Human label for the modal header (the broker's display name). */
  brokerName: string;
};

interface ModalCtx {
  open: (payload: ExploreBrokerPayload) => void;
  close: () => void;
  isOpen: boolean;
}

const ctx = createContext<ModalCtx | null>(null);

export function useExploreBrokerModal(): ModalCtx {
  const v = useContext(ctx);
  if (!v) {
    throw new Error('useExploreBrokerModal must be used inside <ExploreBrokerProvider>');
  }
  return v;
}

export function ExploreBrokerProvider({ children }: { children: ReactNode }) {
  const [payload, setPayload] = useState<ExploreBrokerPayload | null>(null);
  /* Mounted above <Outlet> in Shell, so a navigation from inside the modal
   * would otherwise repaint the page behind it and strand the scroll lock.
   * See lib/useCloseOnNavigation.ts. */
  useCloseOnNavigation(() => setPayload(null));
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
        <ExploreBrokerModal payload={payload} onClose={() => setPayload(null)} />
      )}
    </ctx.Provider>
  );
}

function ExploreBrokerModal({
  payload,
  onClose,
}: {
  payload: ExploreBrokerPayload;
  onClose: () => void;
}) {
  const initialFilters = useMemo(() => browseFiltersForBroker(payload), [payload]);
  const view = useMemoryBrowseState({ filters: initialFilters });

  /* Escape, the focus trap, initial + restored focus and the body scroll lock
   * all come from <Dialog> (lib/useDialog.ts). */
  const browseHref = browseUrlFromState({
    filters: view.filters,
    sort: view.sort,
    tab: view.tab,
    overlay: view.overlay,
  });

  return (
    <Dialog
      open
      onClose={onClose}
      label="Explore broker"
      className="w-[96vw] max-w-[1600px] h-[90vh] flex flex-col"
    >
      <header className="flex items-center justify-between gap-4 px-6 py-3 border-b border-[var(--color-rule)] shrink-0">
        <div className="min-w-0">
          <p className="text-[0.62rem] tracking-[0.22em] uppercase text-[var(--color-ink-3)]">
            Explore broker
          </p>
          <h2
            className="mt-0.5 text-[1.1rem] leading-tight truncate"
            style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
          >
            {payload.brokerName}
          </h2>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {/* A real <Link>, like its sibling. The hand-rolled guard this
            * replaces tested `e.button === 1` for middle-click — which fires
            * `auxclick`, never `click`, so the arm was dead code — and omitted
            * altKey, swallowing "download this link" into an SPA navigation.
            * <Link> owns those rules; closing is the provider's job now
            * (lib/useCloseOnNavigation.ts), so no onClick belongs here. */}
          <Link
            to={browseHref}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)] transition-colors"
            title="Open the full Browse page scoped to this broker"
          >
            <span>Go to Browse</span>
            <span aria-hidden>→</span>
          </Link>
          <DialogClose onClick={onClose} />
        </div>
      </header>
      <div className="flex-1 min-h-0">
        <BrowseExperience
          view={view}
          layout="modal"
          features={{
            presetBar: false,
            mergeMode: false,
            watchdog: false,
            title: false,
            sidebar: false,
            stats: false,
          }}
        />
      </div>
    </Dialog>
  );
}
