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
  { id: 42, label: 'podklad - katastrální mapa', positive: 300, negative: 10009, excluded: 2, reserve: 536,
    sample: 0, sample_reviewed: 0 },
  { id: 2, label: 'exterier - domovní vchod', positive: 173, negative: 10153, excluded: 218, reserve: 0,
    sample: 0, sample_reviewed: 0 },
];
const SAMPLED_HEADS = [{ ...HEADS[0], sample: 1000, sample_reviewed: 40 }, HEADS[1]];

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
    expect(screen.getByTestId('tray-count-reserve')).toHaveTextContent('536');
  });

  it('the reserve tray is the positives the operator has NOT admitted', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    await user.click(screen.getByRole('button', { name: /Reserve/ }));
    await waitFor(() => expect(lastQuery()).toEqual(
      { tag_id: 42, state: 'positive', in_training: false, limit: 50, offset: 0 },
    ));
  });

  /* Filtering these two trays by in_training was a real bug: the backfill never
   * admitted a left-out, so "Left out" showed 4 rows under a count of 1,064.
   * Membership is a question about a POSITIVE only. */
  it('negative and left-out trays read their own state, unfiltered by membership', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    await user.click(screen.getByRole('button', { name: /Training · negative/ }));
    await waitFor(() => expect(lastQuery()).toEqual({ tag_id: 42, state: 'negative', limit: 50, offset: 0 }));
    await user.click(screen.getByRole('button', { name: /Left out/ }));
    await waitFor(() => expect(lastQuery()).toEqual({ tag_id: 42, state: 'excluded', limit: 50, offset: 0 }));
  });

  it('offers no membership move on the negative tray — a negative comes out by re-marking', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    expect(screen.getByTestId('move-11')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /Training · negative/ }));
    await waitFor(() => expect(screen.queryByTestId('move-11')).toBeNull());
    expect(screen.queryByTestId('move-page')).toBeNull();
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

  /* Ten thousand negatives is not a reviewable number. The draw is random, the
   * lane keeps the order the negatives already had, and — the property the
   * whole thing rests on — the drawn list does not move while it is reviewed. */
  describe('the review sample', () => {
    it('offers a draw when there is none, and no lane to open', async () => {
      renderPage();
      await screen.findByTestId('training-tile-11');
      expect(screen.getByTestId('draw-sample')).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /Review sample/ })).toBeNull();
    });

    it('draws at random from the negatives and opens the lane', async () => {
      const user = userEvent.setup();
      vi.mocked(api.drawReviewSample).mockResolvedValue({
        data: { tag_id: 42, state: 'negative', drawn: 1000 },
      });
      renderPage();
      await screen.findByTestId('training-tile-11');
      await user.click(screen.getByTestId('draw-sample'));
      await waitFor(() => expect(api.drawReviewSample).toHaveBeenCalledWith(
        42, { state: 'negative', size: 1000, replace: false },
      ));
      // The lane asks for the drawn rows and NO state — a re-marked photo must
      // keep its place instead of dropping out of the list mid-review.
      await waitFor(() => expect(lastQuery()).toEqual(
        { tag_id: 42, sampled: true, limit: 50, offset: 0 },
      ));
    });

    it('shows progress on the lane and never shrinks the drawn list', async () => {
      const user = userEvent.setup();
      /* The server counts a decided photo as reviewed and leaves the draw
       * alone; the mock does the same, so the refetch after the write cannot
       * paper over a wrong optimistic patch. */
      let reviewed = 40;
      vi.mocked(api.listTrainingSetHeads).mockImplementation(async () => ({
        data: [{ ...HEADS[0], sample: 1000, sample_reviewed: reviewed }, HEADS[1]] as never,
      }));
      vi.mocked(api.setNewDedupTagAnnotation).mockImplementation(async () => {
        reviewed += 1;
        return { data: {} } as never;
      });
      renderPage(['/new-dedup/training-set?tag=42&set=sample']);
      await screen.findByTestId('training-tile-11');
      expect(screen.getByTestId('tray-count-sample')).toHaveTextContent('40/1000');

      await user.click(screen.getByRole('button', { name: 'positive 11' }));
      await waitFor(() => expect(screen.getByTestId('tray-count-sample')).toHaveTextContent('41/1000'));
      // 1000 is a list, not a tray: deciding one does not remove it.
      expect(screen.getByTestId('tray-count-sample')).not.toHaveTextContent('999');
      expect(screen.getByTestId('training-tile-11')).toBeInTheDocument();
    });

    it('asks before a redraw, because it discards a half-reviewed list', async () => {
      const user = userEvent.setup();
      vi.mocked(api.listTrainingSetHeads).mockResolvedValue({ data: SAMPLED_HEADS as never });
      const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
      renderPage(['/new-dedup/training-set?tag=42&set=sample']);
      await screen.findByTestId('draw-again');
      await user.click(screen.getByTestId('draw-again'));
      expect(confirm).toHaveBeenCalled();
      expect(api.drawReviewSample).not.toHaveBeenCalled();
      confirm.mockRestore();
    });
  });

  /* A link from outside names a head and a photo, never a page number: the
   * server resolves which tray and which row, so the offset cannot drift from
   * what the grid renders. */
  describe('deep link to one photo', () => {
    it('lands on the tray and page the server says, and rings the tile', async () => {
      vi.mocked(api.locateTrainingImage).mockResolvedValue({
        data: { tag_id: 42, image_id: 12, tray: 'reserve', state: 'positive',
                in_training: false, rank: 137 },
      });
      renderPage(['/new-dedup/training-set?tag=42&image=12']);
      await waitFor(() => expect(lastQuery()).toEqual(
        { tag_id: 42, state: 'positive', in_training: false, limit: 50, offset: 100 },
      ));
      expect(await screen.findByTestId('deep-link-note')).toHaveTextContent('Reserve');
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
      'Trains on 300 positives and 10009 negatives · 536 waiting in reserve · 2 left out');
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
    const after = { ...HEADS[0], positive: 301, reserve: 535 };
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
    expect(screen.getByTestId('tray-count-reserve')).toHaveTextContent('535');
  });

  it('returns an admitted photo to the reserve', async () => {
    const after = { ...HEADS[0], positive: 299, reserve: 537 };
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
    expect(screen.getByTestId('tray-count-reserve')).toHaveTextContent('537');
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
    await user.click(screen.getByRole('button', { name: /Reserve/ }));
    await user.click(screen.getByRole('button', { name: /Training · negative/ }));
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
    expect(screen.getByTestId('tray-count-negative')).toHaveTextContent('10010');
    expect(tile).toHaveAttribute('data-state', 'negative');
    expect(within(tile).getByText('yours')).toBeInTheDocument();
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
    expect(within(tile).getByText('machine')).toBeInTheDocument();
  });
});

describe('<NewDedupTrainingSet> reading the page', () => {
  it('shows the whole photo and names who decided', async () => {
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    expect(within(tile).getByRole('img').className).toContain('object-contain');
    expect(within(tile).getByText('machine')).toBeInTheDocument();
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
