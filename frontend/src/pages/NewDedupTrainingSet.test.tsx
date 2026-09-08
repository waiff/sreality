/* The training-set page: four trays over STORED membership (migration 484).
 * The property that matters most is the operator's: the training set changes
 * only when they change it — nothing moves in or out of it on its own. */

import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import NewDedupTrainingSet from './NewDedupTrainingSet';
import * as api from '@/lib/api';

vi.mock('@/lib/api');
vi.mock('@/lib/imageUrl', () => ({ imageSrc: () => 'blob:photo' }));

const HEADS = [
  { id: 42, label: 'podklad - katastrální mapa', positive: 300, positive_reserve: 536,
    negative: 1000, negative_reserve: 9009, excluded: 2, review_state: 'not_ready' },
  { id: 2, label: 'exterier - domovní vchod', positive: 173, positive_reserve: 0,
    negative: 1000, negative_reserve: 9153, excluded: 218, review_state: 'ready' },
];

const ROWS = [
  { image_id: 11, storage_path: 'img/1/11.jpg', state: 'positive', source: 'machine',
    excluded_reason: null, updated_at: null, definition_version: 9, definition_stale: false,
    note_id: null, note: null, in_training: true },
  { image_id: 12, storage_path: 'img/1/12.jpg', state: 'positive', source: 'human',
    excluded_reason: null, updated_at: null, definition_version: 8, definition_stale: true,
    note_id: 36, note: 'front shot, building is the subject', in_training: true },
];

const RESERVE_ROWS = [
  { ...ROWS[0], image_id: 301, in_training: false },
  { ...ROWS[0], image_id: 302, in_training: false },
];

function renderPage(entries = ['/new-dedup/training-set']) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={entries}><NewDedupTrainingSet /></MemoryRouter>
    </QueryClientProvider>,
  );
}

const lastQuery = () => vi.mocked(api.listTrainingSet).mock.calls.at(-1)?.[0];

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.listTrainingSetHeads).mockResolvedValue({ data: HEADS as never });
  vi.mocked(api.listTrainingSet).mockResolvedValue({
    data: { rows: ROWS as never, counts: HEADS[0] as never, limit: 50, offset: 0 },
  });
  vi.mocked(api.setNewDedupTagAnnotation).mockResolvedValue({ data: {} } as never);
  vi.mocked(api.setTrainingMembership).mockImplementation(async (t, ids, into) => ({
    data: { tag_id: t, in_training: into, moved: ids, requested: ids.length },
  }));
});

describe('<NewDedupTrainingSet> four trays over stored membership', () => {
  it('opens on the training positives — admitted rows only, not every positive', async () => {
    renderPage();
    await waitFor(() => expect(api.listTrainingSet).toHaveBeenCalledWith(
      { tag_id: 42, state: 'positive', in_training: true, limit: 50, offset: 0 },
    ));
    expect(screen.getByTestId('tray-count-positive')).toHaveTextContent('300');
    expect(screen.getByTestId('tray-count-positive_reserve')).toHaveTextContent('536');
  });

  it('the reserve tray is the positives the operator has NOT admitted', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    await user.click(screen.getByRole('button', { name: /Positive reserve/ }));
    await waitFor(() => expect(lastQuery()).toEqual(
      { tag_id: 42, state: 'positive', in_training: false, limit: 50, offset: 0 },
    ));
  });

  /* 484 filtered every non-reserve tray by in_training, which hid 1,060
   * left-outs the backfill never admitted. 486 keeps the fix for left-outs —
   * they train nothing whichever way the flag points — and gives negatives a
   * real membership, so they are filtered like positives are. */
  it('reads left out by state alone, since membership means nothing there', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    await user.click(screen.getByRole('button', { name: /Left out/ }));
    await waitFor(() => expect(lastQuery()).toEqual(
      { tag_id: 42, state: 'excluded', limit: 50, offset: 0 },
    ));
  });

  it('offers the membership move on both signs, and never on left out', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    expect(screen.getByTestId('move-11')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /Training negative/ }));
    await waitFor(() => expect(screen.getByTestId('move-11')).toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: /Left out/ }));
    await waitFor(() => expect(screen.queryByTestId('move-11')).toBeNull());
    expect(screen.queryByTestId('move-page')).toBeNull();
  });

  it('carries the marks into the focus view, and stays open after one', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    await user.click(screen.getByTestId('open-11'));
    await screen.findByRole('dialog');
    // Deciding is why the photo is open; closing it to reach the marks would
    // make one judgement three clicks.
    expect(screen.getByTestId('zoom-positive')).toHaveAttribute('aria-pressed', 'true');
    await user.click(screen.getByTestId('zoom-negative'));
    await waitFor(() => expect(api.setNewDedupTagAnnotation)
      .toHaveBeenCalledWith(42, 11, 'negative', null));
    // The viewer stays open so the arrow keys walk straight to the next one.
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId('zoom-negative')).toHaveAttribute('aria-pressed', 'true'));
  });

  it('offers no move control in the focus view where membership means nothing', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    await user.click(screen.getByTestId('open-11'));
    expect(await screen.findByTestId('zoom-move')).toHaveTextContent('return to reserve');
    await user.keyboard('{Escape}');
    await user.click(screen.getByRole('button', { name: /Left out/ }));
    await screen.findByTestId('training-tile-11');
    await user.click(screen.getByTestId('open-11'));
    await screen.findByRole('dialog');
    expect(screen.queryByTestId('zoom-move')).toBeNull();
  });

  /* The operator's mapping: left hand on the marks, right hand on the arrows. */
  it('marks by keystroke — a / s / d — through the same path as a click', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    await user.click(screen.getByTestId('open-11'));
    await screen.findByRole('dialog');

    await user.keyboard('s');
    await waitFor(() => expect(api.setNewDedupTagAnnotation)
      .toHaveBeenCalledWith(42, 11, 'negative', null));
    await user.keyboard('d');
    await waitFor(() => expect(api.setNewDedupTagAnnotation)
      .toHaveBeenCalledWith(42, 11, 'excluded', 'pruned'));
    await user.keyboard('a');
    await waitFor(() => expect(api.setNewDedupTagAnnotation)
      .toHaveBeenCalledWith(42, 11, 'positive', null));
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('space moves membership, and arrow keys still walk the page', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    await user.click(screen.getByTestId('open-11'));
    await screen.findByRole('dialog');
    await user.keyboard(' ');
    await waitFor(() => expect(api.setTrainingMembership).toHaveBeenCalledWith(42, [11], false));
    // The arrows keep working, and the keystroke follows the photo on show.
    await user.keyboard('{ArrowRight}s');
    await waitFor(() => expect(api.setNewDedupTagAnnotation)
      .toHaveBeenCalledWith(42, 12, 'negative', null));
  });

  /* The hazard space brings and backspace did not: a focused button activates
   * on it. After one click on a mark, the next space must move membership and
   * NOT re-fire that mark. */
  it('space means one thing even with a mark button focused', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    await user.click(screen.getByTestId('open-11'));
    await screen.findByRole('dialog');
    await user.click(screen.getByTestId('zoom-negative'));
    await waitFor(() => expect(api.setNewDedupTagAnnotation).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId('zoom-negative')).toHaveFocus();

    await user.keyboard(' ');
    await waitFor(() => expect(api.setTrainingMembership).toHaveBeenCalledTimes(1));
    // The focused button did NOT also activate.
    expect(api.setNewDedupTagAnnotation).toHaveBeenCalledTimes(1);
  });

  it('a keystroke in a text field is typing, not a decision', async () => {
    const user = userEvent.setup();
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    // A note field on the page, with no viewer open: the listener must ignore it.
    await user.click(within(tile).getByRole('button', { name: /^negative 11$/ }));
    await waitFor(() => expect(api.setNewDedupTagAnnotation).toHaveBeenCalledTimes(1));
    await user.type(within(tile).getByRole('textbox', { name: /why 11/ }), 'sad');
    expect(api.setNewDedupTagAnnotation).toHaveBeenCalledTimes(1);
  });

  it('opens the shared full-size viewer on a tile, and closes it', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    expect(screen.queryByRole('dialog')).toBeNull();
    await user.click(screen.getByTestId('open-11'));
    const viewer = await screen.findByRole('dialog');
    expect(viewer).toHaveAttribute('aria-modal', 'true');
    await user.click(screen.getByRole('button', { name: /close/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  /* Where the operator got to. Three-valued on purpose: "skipped" is a decision
   * and must not read as "nobody has said" (migration 487). */
  describe('review state', () => {
    it('offers all three, marks the current one, and writes only that field', async () => {
      const user = userEvent.setup();
      let state = 'not_ready';
      vi.mocked(api.listTrainingSetHeads).mockImplementation(async () => ({
        data: [{ ...HEADS[0], review_state: state }, HEADS[1]] as never,
      }));
      vi.mocked(api.setNewDedupTagFlags).mockImplementation(async (_id, flags) => {
        state = flags.review_state ?? state;
        return { data: {} } as never;
      });
      renderPage();
      const group = await screen.findByRole('radiogroup', { name: 'review state' });
      expect(within(group).getAllByRole('radio')).toHaveLength(3);
      expect(screen.getByTestId('review-not_ready')).toHaveAttribute('aria-checked', 'true');

      await user.click(screen.getByTestId('review-skipped'));
      // Only review_state travels: priority is another flag on the same
      // endpoint and setting one must never clobber the other.
      expect(vi.mocked(api.setNewDedupTagFlags).mock.calls[0][1]).toEqual({ review_state: 'skipped' });
      await waitFor(() =>
        expect(screen.getByTestId('review-skipped')).toHaveAttribute('aria-checked', 'true'));
    });

    it('puts the previous state back when the write fails — not a default', async () => {
      const user = userEvent.setup();
      vi.mocked(api.listTrainingSetHeads).mockResolvedValue({
        data: [{ ...HEADS[0], review_state: 'skipped' }, HEADS[1]] as never,
      });
      vi.mocked(api.setNewDedupTagFlags).mockRejectedValue(new Error('nope'));
      renderPage();
      await screen.findByTestId('review-ready');
      await user.click(screen.getByTestId('review-ready'));
      // Rolls back to SKIPPED, the value it had — an error must not quietly
      // reset a deliberate decision to the default.
      await waitFor(() =>
        expect(screen.getByTestId('review-skipped')).toHaveAttribute('aria-checked', 'true'));
    });

    it('marks ready and skipped heads differently in the picker', async () => {
      vi.mocked(api.listTrainingSetHeads).mockResolvedValue({
        data: [{ ...HEADS[0], review_state: 'skipped' }, HEADS[1]] as never,
      });
      renderPage();
      await screen.findByTestId('review-skipped');
      expect(screen.getByRole('option', { name: /katastrální mapa/ })).toHaveTextContent('–');
      expect(screen.getByRole('option', { name: /domovní vchod/ })).toHaveTextContent('✓');
    });
  });

  /* Migration 486: membership is a fact about a LABEL, not about a positive.
   * Each sign has a training set and a reserve; the drawn thousand IS the
   * training negative set, so there is no sample and no separate table. */
  describe('the draw', () => {
    it('names four trays by sign and membership, plus left out', async () => {
      renderPage();
      await screen.findByTestId('training-tile-11');
      for (const name of ['Training positive', 'Positive reserve', 'Training negative',
                          'Negative reserve', 'Left out']) {
        expect(screen.getByRole('button', { name: new RegExp(name) })).toBeInTheDocument();
      }
      expect(screen.queryByRole('button', { name: /Review sample/ })).toBeNull();
    });

    it('reads each tray as one state plus membership', async () => {
      const user = userEvent.setup();
      renderPage();
      await screen.findByTestId('training-tile-11');
      await user.click(screen.getByRole('button', { name: /Negative reserve/ }));
      await waitFor(() => expect(lastQuery()).toEqual(
        { tag_id: 42, state: 'negative', in_training: false, limit: 50, offset: 0 },
      ));
      await user.click(screen.getByRole('button', { name: /Training negative/ }));
      await waitFor(() => expect(lastQuery()).toEqual(
        { tag_id: 42, state: 'negative', in_training: true, limit: 50, offset: 0 },
      ));
    });

    it('draws negatives into training and confirms first, since it replaces a set', async () => {
      const user = userEvent.setup();
      const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
      vi.mocked(api.drawTrainingSet).mockResolvedValue({
        data: { tag_id: 42, state: 'negative', drawn: 1000 },
      });
      renderPage();
      await screen.findByTestId('training-tile-11');
      await user.click(screen.getByTestId('draw-negatives'));
      expect(confirm).toHaveBeenCalled();
      await waitFor(() => expect(api.drawTrainingSet).toHaveBeenCalledWith(
        42, { state: 'negative', size: 1000 },
      ));
      confirm.mockRestore();
    });

    it('shows a tray count and nothing about who decided it', async () => {
      renderPage();
      await screen.findByTestId('training-tile-11');
      expect(screen.getByTestId('tray-count-negative')).toHaveTextContent('1000');
      expect(screen.getByTestId('tray-count-negative_reserve')).toHaveTextContent('9009');
      const tile = screen.getByTestId('training-tile-11');
      for (const word of ['machine', 'yours']) {
        expect(within(tile).queryByText(word)).toBeNull();
      }
    });

    it('still answers a link written before the rename', async () => {
      renderPage(['/new-dedup/training-set?tag=42&set=reserve']);
      await waitFor(() => expect(lastQuery()).toEqual(
        { tag_id: 42, state: 'positive', in_training: false, limit: 50, offset: 0 },
      ));
    });
  });

  /* A link from outside names a head and a photo, never a page number: the
   * server resolves which tray and which row, so the offset cannot drift from
   * what the grid renders. */
  describe('deep link to one photo', () => {
    it('lands on the tray and page the server says, and rings the tile', async () => {
      vi.mocked(api.locateTrainingImage).mockResolvedValue({
        data: { tag_id: 42, image_id: 12, tray: 'positive_reserve', state: 'positive',
                in_training: false, rank: 137 },
      });
      renderPage(['/new-dedup/training-set?tag=42&image=12']);
      await waitFor(() => expect(lastQuery()).toEqual(
        { tag_id: 42, state: 'positive', in_training: false, limit: 50, offset: 100 },
      ));
      expect(await screen.findByTestId('deep-link-note')).toHaveTextContent('Positive reserve');
      expect(screen.getByTestId('training-tile-12')).toHaveAttribute('data-linked', 'true');
      expect(screen.getByTestId('training-tile-11')).not.toHaveAttribute('data-linked');
    });

    it('says so and stays put when the head has no label for that photo', async () => {
      vi.mocked(api.locateTrainingImage).mockRejectedValue(new Error('404'));
      renderPage(['/new-dedup/training-set?tag=42&image=999']);
      expect(await screen.findByTestId('deep-link-note')).toHaveTextContent('no label on this head');
      expect(lastQuery()).toEqual(
        { tag_id: 42, state: 'positive', in_training: true, limit: 50, offset: 0 },
      );
    });

    it('clears the link without moving the page', async () => {
      const user = userEvent.setup();
      vi.mocked(api.locateTrainingImage).mockResolvedValue({
        data: { tag_id: 42, image_id: 11, tray: 'positive', state: 'positive',
                in_training: true, rank: 0 },
      });
      renderPage(['/new-dedup/training-set?tag=42&image=11']);
      await screen.findByTestId('deep-link-note');
      await user.click(screen.getByTestId('deep-link-clear'));
      await waitFor(() => expect(screen.queryByTestId('deep-link-note')).toBeNull());
      expect(screen.getByTestId('training-tile-11')).not.toHaveAttribute('data-linked');
    });
  });

  it('offers a 10000-per-page step', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    await user.click(screen.getByRole('button', { name: '10000' }));
    await waitFor(() => expect(lastQuery()).toEqual(expect.objectContaining({ limit: 10000, offset: 0 })));
  });

  it('has no anyone / machine / yours breakdown', async () => {
    renderPage();
    await screen.findByTestId('training-tile-11');
    expect(screen.queryByRole('group', { name: 'decided by' })).toBeNull();
    for (const name of ['Anyone', 'The machine’s', 'Yours']) {
      expect(screen.queryByRole('button', { name })).toBeNull();
    }
    expect(screen.getByTestId('head-summary')).toHaveTextContent(
      'Trains on 300 positives and 1000 negatives · 536 + 9009 in reserve · 2 left out');
  });

  it('states that the set changes only when the operator changes it', async () => {
    renderPage();
    expect((await screen.findAllByText(/changes only when you change it/)).length).toBeGreaterThan(0);
    expect(screen.getByText(/sealed exam images are excluded/)).toBeInTheDocument();
  });
});

describe('<NewDedupTrainingSet> moving between reserve and the training set', () => {
  it('admits one reserve photo, and the two counts move at once', async () => {
    vi.mocked(api.listTrainingSet).mockResolvedValue({
      data: { rows: RESERVE_ROWS as never, counts: HEADS[0] as never, limit: 50, offset: 0 },
    });
    // The reconciling refetch returns the server's truth, which in production
    // agrees with the optimistic patch — so the mock moves too.
    const after = { ...HEADS[0], positive: 301, positive_reserve: 535 };
    vi.mocked(api.listTrainingSetHeads)
      .mockResolvedValueOnce({ data: HEADS as never })
      .mockResolvedValue({ data: [after, HEADS[1]] as never });
    const user = userEvent.setup();
    renderPage(['/new-dedup/training-set?set=reserve']);
    const tile = await screen.findByTestId('training-tile-301');
    expect(within(tile).getByTestId('move-301')).toHaveTextContent('move to training');
    await user.click(within(tile).getByTestId('move-301'));
    await waitFor(() => expect(api.setTrainingMembership).toHaveBeenCalledWith(42, [301], true));
    await waitFor(() => expect(screen.getByTestId('tray-count-positive')).toHaveTextContent('301'));
    expect(screen.getByTestId('tray-count-positive_reserve')).toHaveTextContent('535');
  });

  it('returns an admitted photo to the reserve', async () => {
    const after = { ...HEADS[0], positive: 299, positive_reserve: 537 };
    vi.mocked(api.listTrainingSetHeads)
      .mockResolvedValueOnce({ data: HEADS as never })
      .mockResolvedValue({ data: [after, HEADS[1]] as never });
    const user = userEvent.setup();
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    expect(within(tile).getByTestId('move-11')).toHaveTextContent('return to reserve');
    await user.click(within(tile).getByTestId('move-11'));
    await waitFor(() => expect(api.setTrainingMembership).toHaveBeenCalledWith(42, [11], false));
    await waitFor(() => expect(screen.getByTestId('tray-count-positive')).toHaveTextContent('299'));
    expect(screen.getByTestId('tray-count-positive_reserve')).toHaveTextContent('537');
  });

  it('moves a whole page, chunked, and previews exactly what it will take', async () => {
    const rows = Array.from({ length: 450 }, (_, i) => ({ ...RESERVE_ROWS[0], image_id: 1000 + i }));
    vi.mocked(api.listTrainingSet).mockResolvedValue({
      data: { rows: rows as never, counts: HEADS[0] as never, limit: 500, offset: 0 },
    });
    const user = userEvent.setup();
    renderPage(['/new-dedup/training-set?set=reserve&n=500']);
    const btn = await screen.findByTestId('move-page');
    expect(btn).toHaveTextContent('Move all 450 on this page into the training set');
    await user.hover(btn);
    expect(screen.getByTestId('training-tile-1000')).toHaveAttribute('data-previewed', 'true');
    await user.click(btn);
    await waitFor(() => expect(api.setTrainingMembership).toHaveBeenCalledTimes(3));
    expect(vi.mocked(api.setTrainingMembership).mock.calls.map((c) => c[1].length)).toEqual([200, 200, 50]);
  });

  it('nothing enters or leaves the training set without an explicit move', async () => {
    // The operator's correction: a reviewed set stays the size it was reviewed
    // at. Loading, paging and switching trays must never call the move route.
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    await user.click(screen.getByRole('button', { name: /Positive reserve/ }));
    await user.click(screen.getByRole('button', { name: /Training negative/ }));
    expect(api.setTrainingMembership).not.toHaveBeenCalled();
  });
});

describe('<NewDedupTrainingSet> changing a mark', () => {
  it('writes a human label, admits the row, and moves the tray counts at once', async () => {
    let resolve: (v: unknown) => void = () => {};
    vi.mocked(api.setNewDedupTagAnnotation).mockReturnValue(new Promise((r) => { resolve = r; }) as never);
    const user = userEvent.setup();
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    await user.click(within(tile).getByRole('button', { name: /^negative 11$/ }));
    // Still pending — the numbers have already moved.
    expect(screen.getByTestId('tray-count-positive')).toHaveTextContent('299');
    expect(screen.getByTestId('tray-count-negative')).toHaveTextContent('1001');
    expect(tile).toHaveAttribute('data-state', 'negative');
    resolve({ data: {} });
    await waitFor(() => expect(api.setNewDedupTagAnnotation).toHaveBeenCalledWith(42, 11, 'negative', null));
  });

  it('a leave-out carries the pruned reason', async () => {
    const user = userEvent.setup();
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    await user.click(within(tile).getByRole('button', { name: /^excluded 11$/ }));
    await waitFor(() => expect(api.setNewDedupTagAnnotation).toHaveBeenCalledWith(42, 11, 'excluded', 'pruned'));
  });

  it('a failed change puts the tile back', async () => {
    vi.mocked(api.setNewDedupTagAnnotation).mockRejectedValue(new Error('boom'));
    const user = userEvent.setup();
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    await user.click(within(tile).getByRole('button', { name: /^negative 11$/ }));
    await waitFor(() => expect(tile).toHaveAttribute('data-state', 'positive'));
  });
});

describe('<NewDedupTrainingSet> reading the page', () => {
  /* No "machine / yours" on a tile: the operator's ruling is that a label in a
   * tray is a label, and a set of a thousand is a thousand. The human-wins rail
   * still holds in the database; it is not a thing to read on every photo. */
  it('shows the whole photo and says nothing about who decided it', async () => {
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    expect(within(tile).getByRole('img').className).toContain('object-contain');
    expect(within(tile).queryByText('machine')).toBeNull();
    expect(within(tile).queryByText('yours')).toBeNull();
    expect(within(screen.getByTestId('training-tile-12')).getByText(/old wording/)).toBeInTheDocument();
  });

  it('pages by the chosen size and states the tray total', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    expect(screen.getByTestId('page-range')).toHaveTextContent('1–2 of 300');
    await user.click(within(screen.getByRole('group', { name: 'per page' })).getByRole('button', { name: '500' }));
    await waitFor(() => expect(lastQuery()).toEqual(expect.objectContaining({ limit: 500, offset: 0 })));
  });

  it('renders typographic characters, not their escape codes', async () => {
    renderPage();
    await screen.findByTestId('training-tile-11');
    expect(document.body.textContent).not.toMatch(/\\u20/);
  });
});

describe('<NewDedupTrainingSet> notes', () => {
  it('offers a note field on a changed tile and sends it with from/to', async () => {
    const user = userEvent.setup();
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    await user.click(within(tile).getByRole('button', { name: /^negative 11$/ }));
    const form = await screen.findByTestId('note-form-11');
    await user.type(within(form).getByRole('textbox'), 'not a cadastral map');
    await user.click(within(form).getByRole('button', { name: 'save' }));
    await waitFor(() => expect(api.setNewDedupTagAnnotation).toHaveBeenLastCalledWith(
      42, 11, 'negative', null, { text: 'not a cadastral map', from_state: 'positive' },
    ));
  });

  it('shows a saved note with edit and remove', async () => {
    vi.mocked(api.editTagLabelNote).mockResolvedValue({ data: { id: 36, image_id: 12, note: 'must be frontal' } as never });
    const user = userEvent.setup();
    renderPage();
    const tile = await screen.findByTestId('training-tile-12');
    expect(within(tile).getByTestId('note-saved-12')).toHaveTextContent('front shot, building is the subject');
    await user.click(within(tile).getByRole('button', { name: 'edit note' }));
    const input = screen.getByLabelText('why 12');
    await user.clear(input);
    await user.type(input, 'must be frontal');
    await user.click(within(screen.getByTestId('note-form-12')).getByRole('button', { name: 'save' }));
    await waitFor(() => expect(api.editTagLabelNote).toHaveBeenCalledWith(36, 'must be frontal'));
  });
});
