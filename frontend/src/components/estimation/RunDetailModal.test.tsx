/* THE NESTED PAIR — the app's only shipped one, and the case the W6a layer
 * stack exists for.
 *
 * RunDetailModal ("Show estimation detail") renders a comparables table, and a
 * row of that table opens ComparableModal INSIDE it. Before W6b each of the two
 * kept its own `document` keydown listener, so a single Escape reached BOTH and
 * closed BOTH — the operator lost the run report they had opened the comparable
 * from. A click on the outer backdrop, still reachable behind the inner card,
 * did the same thing.
 *
 * The assertions below ask the LAYER STACK who is on top (openDialogLayerCount
 * / topDialogPanel), not the rendered DOM. That distinction is the point: while
 * both dialogs are open both are rendered, so every assertion phrased as "is it
 * still on screen" was equally true before and after the fix, and the ordering
 * bug hid behind exactly that heuristic.
 *
 * The whole-contract assertions (focus in, Tab wrap, focus restore, scroll
 * lock) come from the shared expectDialogContract; ComparableModal's own file
 * holds the same for the inner dialog on its own.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { expectDialogContract } from '@/test/a11y';
import {
  MODAL_Z_BASE,
  openDialogLayerCount,
  topDialogPanel,
} from '@/lib/useDialog';
import { RunDetailModal } from './RunPanel';
import type { EstimationRun } from '@/lib/types';

const COMPARABLE_LISTING_ID = 4242;

/* The comparable is listing_id-only (sreality_id NULL), which is the shape
 * that also switches OFF the summaries and legacy-image queries — the modal
 * under test is the subject here, not the hydration fan-out. */
vi.mock('@/lib/queries', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/queries')>();
  const listing = {
    id: 4242,
    sreality_id: null,
    source: 'bezrealitky',
    source_id_native: 'br-1',
    price_czk: 5_400_000,
    area_m2: 62,
    price_per_m2: 87_096,
    price_per_m2_basis: 'sale_floor_area',
    category_main: 'byt',
    category_type: 'prodej',
    disposition: '2+kk',
    locality: 'Praha 3',
    is_active: true,
    last_seen_at: '2026-09-01T00:00:00Z',
  };
  return {
    ...actual,
    fetchListingsForListingIds: async () => new Map([[4242, listing]]),
    fetchListingsByIds: async () => new Map(),
    fetchImagesByListingIds: async () => new Map(),
  };
});

vi.mock('@/lib/hydration', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/hydration')>()),
  useListingPhotos: () => ({ photos: new Map(), isPending: false }),
}));

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  listEstimationFeedback: async () => ({ data: [] }),
  fetchListingSummaries: async () => ({ data: [] }),
}));

const RUN = {
  id: 77,
  created_at: '2026-09-01T10:00:00Z',
  source: 'ui',
  mode: 'agent',
  status: 'success',
  input_url: null,
  input_sreality_id: null,
  input_listing_id: null,
  input_spec: null,
  estimate_kind: 'rent',
  input_purchase_price_czk: null,
  estimated_monthly_rent_czk: 21_000,
  rent_p25_czk: null,
  rent_p75_czk: null,
  estimated_sale_price_czk: null,
  sale_p25_czk: null,
  sale_p75_czk: null,
  gross_yield_pct: null,
  confidence: null,
  comparables_used: [
    {
      listing_id: COMPARABLE_LISTING_ID,
      sreality_id: null,
      snapshot_id: null,
      snapshot_date: null,
      data_age_days: 3,
      verified_during_estimate: true,
    },
  ],
  comparables_excluded: null,
  trace: null,
  warnings: null,
  error_message: null,
  parent_run_id: null,
  rerun_reason: null,
  source_kind: null,
  parse_confidence: null,
  parse_confidence_per_field: null,
  subject_attributes: null,
  cost_usd_total: null,
} as unknown as EstimationRun;

/* Mounting IS opening (lib/useDialog), which is how RunBody opens it:
 * `{detailOpen && <RunDetailModal/>}`. */
function Host() {
  const [open, setOpen] = useState(false);
  // One client per host, not per render: a re-render must not discard the
  // cache and every in-flight query with it.
  const [qc] = useState(() => new QueryClient({ defaultOptions: { queries: { retry: false } } }));
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/estimation/77']}>
        <button type="button" onClick={() => setOpen(true)}>
          open-run-detail
        </button>
        {open && <RunDetailModal run={RUN} onClose={() => setOpen(false)} />}
      </MemoryRouter>
    </QueryClientProvider>
  );
}

const runDetail = () => screen.getByRole('dialog', { name: 'Estimation detail' });
const comparable = () =>
  screen.getByRole('dialog', { name: `Comparable · id ${COMPARABLE_LISTING_ID}` });

/* Open the run report, then a comparable row inside it. The ComparableModal is
 * a lazy chunk behind <Suspense fallback={null}>, so the inner dialog is
 * awaited rather than assumed. */
async function openPair() {
  render(<Host />);
  const trigger = screen.getByRole('button', { name: 'open-run-detail' });
  /* fireEvent.click does not move focus the way a real pointer click does, and
   * focus RESTORE is measured against whatever held focus when the layer
   * mounted — so each trigger is focused first, or the restore assertions
   * below would be measuring document.body. */
  trigger.focus();
  fireEvent.click(trigger);

  const row = await screen.findByRole('button', {
    name: String(COMPARABLE_LISTING_ID),
  });
  row.focus();
  fireEvent.click(row);
  await screen.findByRole('dialog', {
    name: `Comparable · id ${COMPARABLE_LISTING_ID}`,
  });
  return { trigger, row };
}

describe('<RunDetailModal>', () => {
  it('honours the whole modal-dialog contract on its own', async () => {
    render(<Host />);
    const trigger = screen.getByRole('button', { name: 'open-run-detail' });
    // The comparables table hydrates asynchronously; open once and let it
    // settle so the contract runs against the finished panel, then close.
    fireEvent.click(trigger);
    await screen.findByRole('button', { name: String(COMPARABLE_LISTING_ID) });
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(openDialogLayerCount()).toBe(0));

    expectDialogContract({ open: () => fireEvent.click(trigger), trigger });
  });

  it('opens the comparable as a SECOND layer, on top of the run report', async () => {
    await openPair();

    expect(openDialogLayerCount()).toBe(2);
    // Render-phase ordering, not effect-flush ordering: the inner dialog is the
    // top layer even though React flushes a commit's effects child-first.
    expect(topDialogPanel()).toBe(comparable());
    // And it paints there too — the rank is applied imperatively on the
    // outermost fixed element of each layer, before the browser paints.
    expect(runDetail().parentElement?.style.zIndex).toBe(String(MODAL_Z_BASE));
    expect(comparable().parentElement?.style.zIndex).toBe(String(MODAL_Z_BASE + 1));
  });

  it('closes ONE layer per Escape — the comparable first, the run report second', async () => {
    const { trigger, row } = await openPair();

    fireEvent.keyDown(document, { key: 'Escape' });

    // THE REGRESSION. Two unguarded `document` listeners used to make this one
    // press close both.
    await waitFor(() => expect(openDialogLayerCount()).toBe(1));
    expect(runDetail()).toBeInTheDocument();
    expect(
      screen.queryByRole('dialog', {
        name: `Comparable · id ${COMPARABLE_LISTING_ID}`,
      }),
    ).toBeNull();
    // The run report is top again, so it answers the NEXT press.
    expect(topDialogPanel()).toBe(runDetail());
    // Each layer restores focus to ITS own opener: the comparable's is the
    // table row inside the run report, not the button that opened the report.
    expect(document.activeElement).toBe(row);

    fireEvent.keyDown(document, { key: 'Escape' });

    await waitFor(() => expect(openDialogLayerCount()).toBe(0));
    expect(screen.queryByRole('dialog')).toBeNull();
    // One ref-counted lock across both layers: the inner one closing must not
    // have released it early, and the outer one closing restores what was there
    // before the FIRST lock.
    expect(document.body.style.overflow).toBe('');
    expect(document.activeElement).toBe(trigger);
  });

  it('ignores a click on the run report backdrop while the comparable is open', async () => {
    await openPair();

    // Reachable behind the inner card, and it used to dismiss the run report
    // out from under it. Only the frontmost layer answers its backdrop.
    const backdrop = runDetail().parentElement as HTMLElement;
    fireEvent.mouseDown(backdrop);

    expect(openDialogLayerCount()).toBe(2);
    expect(runDetail()).toBeInTheDocument();
    expect(comparable()).toBeInTheDocument();
  });

  it('keeps the floating feedback panel inside the focus trap', async () => {
    // It is `position: fixed` and paints beside the card, but it is a child of
    // the PANEL — as a sibling of it, its controls sat outside the trap's cycle
    // and became unreachable by keyboard the moment the trap arrived.
    render(<Host />);
    fireEvent.click(screen.getByRole('button', { name: 'open-run-detail' }));

    const feedback = await screen.findByRole('button', { name: /Provide feedback/ });
    expect(runDetail().contains(feedback)).toBe(true);
  });
});
