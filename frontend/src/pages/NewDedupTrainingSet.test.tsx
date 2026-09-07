/* The training-set page, no limits: three trays, each ONE server query on one
 * state — exactly what the trainer reads — and counts that move the instant a
 * mark does. A correction writes a HUMAN label, which the store then protects
 * from every later machine pass. */

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
  { id: 3, label: 'exterier - fasáda', positive: 1131, negative: 9166, excluded: 247,
    machine_positive: 1113, human_positive: 18, machine_negative: 9120, human_negative: 46 },
  { id: 2, label: 'exterier - domovní vchod', positive: 173, negative: 10153, excluded: 218,
    machine_positive: 159, human_positive: 14, machine_negative: 10150, human_negative: 3 },
];

const ROWS = [
  { image_id: 11, storage_path: 'img/1/11.jpg', state: 'positive', source: 'machine',
    excluded_reason: null, updated_at: null, definition_version: 9, definition_stale: false,
    note_id: null, note: null },
  { image_id: 12, storage_path: 'img/1/12.jpg', state: 'positive', source: 'human',
    excluded_reason: null, updated_at: null, definition_version: 8, definition_stale: true,
    note_id: 36, note: 'front shot, building is the subject' },
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
});

describe('<NewDedupTrainingSet> three trays, no limits', () => {
  it('opens on the head with the most positives, on the positive tray = every positive', async () => {
    renderPage();
    await waitFor(() => expect(api.listTrainingSet).toHaveBeenCalledWith(
      { tag_id: 3, state: 'positive', limit: 50, offset: 0 },
    ));
    expect(await screen.findByTestId('training-tile-11')).toBeInTheDocument();
    // No cutoff anywhere: the count is the plain count of positives.
    expect(screen.getByTestId('tray-count-positive')).toHaveTextContent('1131');
    expect(screen.queryByText(/reserve/i)).toBeNull();
    expect(screen.queryByLabelText('target')).toBeNull();
  });

  it('the negative tray is EVERY negative, the machine’s included', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    await user.click(screen.getByRole('button', { name: /Training · negative/ }));
    await waitFor(() => expect(lastQuery()).toEqual({ tag_id: 3, state: 'negative', limit: 50, offset: 0 }));
    expect(screen.getByTestId('tray-count-negative')).toHaveTextContent('9166');
  });

  it('the left-out tray is the excluded labels', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    await user.click(screen.getByRole('button', { name: /Left out/ }));
    await waitFor(() => expect(lastQuery()).toEqual({ tag_id: 3, state: 'excluded', limit: 50, offset: 0 }));
    expect(screen.getByTestId('tray-count-excluded')).toHaveTextContent('247');
  });

  it('“decided by” narrows a tray at the server and shows its exact count', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    await user.click(screen.getByRole('button', { name: /The machine’s/ }));
    await waitFor(() => expect(lastQuery()).toEqual({ tag_id: 3, state: 'positive', source: 'machine', limit: 50, offset: 0 }));
    expect(screen.getByTestId('who-count-machine')).toHaveTextContent('1113');
    expect(screen.getByTestId('page-range')).toHaveTextContent('of 1113');
    await user.click(screen.getByRole('button', { name: 'Anyone' }));
    await waitFor(() => expect(lastQuery()).not.toHaveProperty('source'));
  });

  it('switches head and resets the page offset', async () => {
    const user = userEvent.setup();
    renderPage(['/new-dedup/training-set?offset=100']);
    await screen.findByTestId('training-tile-11');
    await user.selectOptions(screen.getByLabelText('head'), '2');
    await waitFor(() => expect(lastQuery()).toEqual(expect.objectContaining({ tag_id: 2, offset: 0 })));
  });

  it('states the holdout is absent and summarises who decided what', async () => {
    renderPage();
    expect(await screen.findByText(/sealed exam images are excluded/)).toBeInTheDocument();
    expect(screen.getByTestId('head-summary')).toHaveTextContent('1131 positives (18 yours, 1113 the machine’s)');
    expect(screen.getByTestId('head-summary')).toHaveTextContent('9166 negatives (46 yours, 9120 the machine’s)');
  });
});

describe('<NewDedupTrainingSet> counts react instantly', () => {
  it('moving a machine positive to negative changes both tray counts before the server answers', async () => {
    // The operator's ask, verbatim: counts up to date and reacting instantly.
    let resolve: (v: unknown) => void = () => {};
    vi.mocked(api.setNewDedupTagAnnotation).mockReturnValue(new Promise((r) => { resolve = r; }) as never);
    const user = userEvent.setup();
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    await user.click(within(tile).getByRole('button', { name: /^negative 11$/ }));
    // Still pending — and the numbers have already moved.
    expect(screen.getByTestId('tray-count-positive')).toHaveTextContent('1130');
    expect(screen.getByTestId('tray-count-negative')).toHaveTextContent('9167');
    expect(screen.getByTestId('head-summary')).toHaveTextContent('1130 positives (18 yours, 1112 the machine’s)');
    expect(screen.getByTestId('head-summary')).toHaveTextContent('9167 negatives (47 yours');
    expect(tile).toHaveAttribute('data-state', 'negative');
    expect(within(tile).getByText('yours')).toBeInTheDocument();
    resolve({ data: {} });
    await waitFor(() => expect(screen.getByTestId('note-form-11')).toBeInTheDocument());
  });

  it('confirming a machine positive keeps the tray count and moves it from the machine’s to yours', async () => {
    // The optimistic patch moves the numbers at once; the reconciling refetch
    // then returns the server's truth, which in production agrees with it —
    // so the mock returns the bumped head after the first load.
    const bumped = { ...HEADS[0], human_positive: 19, machine_positive: 1112 };
    vi.mocked(api.listTrainingSetHeads)
      .mockResolvedValueOnce({ data: HEADS as never })
      .mockResolvedValue({ data: [bumped, HEADS[1]] as never });
    const user = userEvent.setup();
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    await user.click(within(tile).getByRole('button', { name: /^positive 11$/ }));
    await waitFor(() => expect(api.setNewDedupTagAnnotation).toHaveBeenCalledWith(3, 11, 'positive', null));
    expect(screen.getByTestId('tray-count-positive')).toHaveTextContent('1131');
    await waitFor(() => expect(screen.getByTestId('head-summary')).toHaveTextContent('19 yours, 1112 the machine’s'));
  });

  it('a failed move puts the tile and the counts back', async () => {
    vi.mocked(api.setNewDedupTagAnnotation).mockRejectedValue(new Error('boom'));
    const user = userEvent.setup();
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    await user.click(within(tile).getByRole('button', { name: /^excluded 11$/ }));
    await waitFor(() => expect(tile).toHaveAttribute('data-state', 'positive'));
    expect(within(tile).getByText('machine')).toBeInTheDocument();
  });

  it('a moved tile stays in its tray, saying where it went, until the next load', async () => {
    const user = userEvent.setup();
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    await user.click(within(tile).getByRole('button', { name: /^negative 11$/ }));
    await waitFor(() => expect(within(tile).getByText(/Now in/)).toHaveTextContent('Training · negative'));
    expect(api.listTrainingSet).toHaveBeenCalledTimes(1);
  });

  it('a leave-out carries the pruned reason, never a bare excluded', async () => {
    const user = userEvent.setup();
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    await user.click(within(tile).getByRole('button', { name: /^excluded 11$/ }));
    await waitFor(() => expect(api.setNewDedupTagAnnotation).toHaveBeenCalledWith(3, 11, 'excluded', 'pruned'));
  });
});

describe('<NewDedupTrainingSet> confirming the rest of the page', () => {
  it('confirms every untouched machine label on the page, chunked, and moves the who-counts', async () => {
    vi.mocked(api.bulkSetNewDedupTagAnnotation).mockImplementation(async (_t, ids) => ({
      data: { updated: ids.length, tag_id: 3, state: 'positive', excluded_reason: null, image_ids: ids },
    }) as never);
    const bumped = { ...HEADS[0], human_positive: 19, machine_positive: 1112 };
    vi.mocked(api.listTrainingSetHeads)
      .mockResolvedValueOnce({ data: HEADS as never })
      .mockResolvedValue({ data: [bumped, HEADS[1]] as never });
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    const btn = screen.getByTestId('confirm-page');
    expect(btn).toHaveTextContent('Confirm the other 1 on this page');
    await user.hover(btn);
    expect(screen.getByTestId('training-tile-11')).toHaveAttribute('data-previewed', 'true');
    expect(screen.getByTestId('training-tile-12')).not.toHaveAttribute('data-previewed');
    await user.click(btn);
    await waitFor(() => expect(api.bulkSetNewDedupTagAnnotation).toHaveBeenCalledWith(3, [11], 'positive', null));
    await waitFor(() => expect(screen.queryByTestId('confirm-page')).toBeNull());
    await waitFor(() => expect(screen.getByTestId('head-summary')).toHaveTextContent('19 yours, 1112 the machine’s'));
  });

  it('writes in chunks of 200 on a big page', async () => {
    const rows = Array.from({ length: 450 }, (_, i) => ({ ...ROWS[0], image_id: 1000 + i }));
    vi.mocked(api.listTrainingSet).mockResolvedValue({
      data: { rows: rows as never, counts: HEADS[0] as never, limit: 500, offset: 0 },
    });
    vi.mocked(api.bulkSetNewDedupTagAnnotation).mockImplementation(async (_t, ids) => ({
      data: { updated: ids.length, tag_id: 3, state: 'positive', excluded_reason: null, image_ids: ids },
    }) as never);
    const user = userEvent.setup();
    renderPage(['/new-dedup/training-set?n=500']);
    await screen.findByTestId('training-tile-1000');
    await user.click(screen.getByTestId('confirm-page'));
    await waitFor(() => expect(api.bulkSetNewDedupTagAnnotation).toHaveBeenCalledTimes(3));
    expect(vi.mocked(api.bulkSetNewDedupTagAnnotation).mock.calls.map((c) => c[1].length)).toEqual([200, 200, 50]);
  });
});

describe('<NewDedupTrainingSet> reading the page', () => {
  it('shows the whole photo, never a crop, and opens it full-size', async () => {
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    const img = within(tile).getByRole('img');
    expect(img.className).toContain('object-contain');
    expect(img.closest('a')).toHaveAttribute('target', '_blank');
  });

  it('labels the tile buttons in words and names who decided', async () => {
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    expect(within(tile).getByRole('button', { name: /^positive 11$/ })).toHaveTextContent('applies');
    expect(within(tile).getByRole('button', { name: /^negative 11$/ })).toHaveTextContent('no');
    expect(within(tile).getByRole('button', { name: /^excluded 11$/ })).toHaveTextContent('left out');
    expect(within(tile).getByText('machine')).toBeInTheDocument();
    expect(within(screen.getByTestId('training-tile-12')).getByText('yours')).toBeInTheDocument();
    expect(within(screen.getByTestId('training-tile-12')).getByText(/old wording/)).toBeInTheDocument();
  });

  it('names heads in full in the picker', async () => {
    renderPage();
    await screen.findByTestId('training-tile-11');
    expect(screen.getByRole('option', { name: /exterier - domovní vchod · 173/ })).toBeInTheDocument();
  });

  it('renders typographic characters, not their escape codes', async () => {
    renderPage();
    await screen.findByTestId('training-tile-11');
    expect(document.body.textContent).not.toMatch(/\\u20/);
  });

  it('says so when a tray is empty', async () => {
    vi.mocked(api.listTrainingSet).mockResolvedValue({
      data: { rows: [], counts: HEADS[1] as never, limit: 50, offset: 0 },
    });
    renderPage();
    expect(await screen.findByText(/Nothing in this tray/)).toBeInTheDocument();
  });
});

describe('<NewDedupTrainingSet> paging', () => {
  it('offers 50 / 100 / 500 / 2000, sends the choice as the limit, and resets the offset', async () => {
    const user = userEvent.setup();
    renderPage(['/new-dedup/training-set?offset=100']);
    await screen.findByTestId('training-tile-11');
    const group = screen.getByRole('group', { name: 'per page' });
    for (const n of ['50', '100', '500', '2000']) {
      expect(within(group).getByRole('button', { name: n })).toBeInTheDocument();
    }
    await user.click(within(group).getByRole('button', { name: '2000' }));
    await waitFor(() => expect(lastQuery()).toEqual(expect.objectContaining({ limit: 2000, offset: 0 })));
  });

  it('states the exact tray total and jumps to the last page', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    expect(screen.getByTestId('page-range')).toHaveTextContent('1–2 of 1131');
    await user.click(screen.getByTestId('jump-last'));
    await waitFor(() => expect(lastQuery()).toEqual(expect.objectContaining({ offset: 1100, limit: 50 })));
  });
});

describe('<NewDedupTrainingSet> the reason for a change', () => {
  it('offers a note field on a moved tile and sends it with from/to', async () => {
    const user = userEvent.setup();
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    expect(screen.queryByTestId('note-form-11')).toBeNull();
    await user.click(within(tile).getByRole('button', { name: /^negative 11$/ }));
    const form = await screen.findByTestId('note-form-11');
    await user.type(within(form).getByRole('textbox'), 'entrance door, facade is only the backdrop');
    await user.click(within(form).getByRole('button', { name: 'save' }));
    await waitFor(() => expect(api.setNewDedupTagAnnotation).toHaveBeenLastCalledWith(
      3, 11, 'negative', null,
      { text: 'entrance door, facade is only the backdrop', from_state: 'positive' },
    ));
  });

  it('shows a saved note with edit and remove; editing sends a PATCH', async () => {
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
    await waitFor(() => expect(screen.getByTestId('note-saved-12')).toHaveTextContent('must be frontal'));
    expect(api.setNewDedupTagAnnotation).not.toHaveBeenCalled();
  });

  it('a note added without a move re-states the current mark, from null', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    await user.click(screen.getByTestId('note-add-11'));
    await user.type(screen.getByLabelText('why 11'), 'clear frontal facade');
    await user.click(within(screen.getByTestId('note-form-11')).getByRole('button', { name: 'save' }));
    await waitFor(() => expect(api.setNewDedupTagAnnotation).toHaveBeenCalledWith(
      3, 11, 'positive', null, { text: 'clear frontal facade', from_state: null },
    ));
  });
});
