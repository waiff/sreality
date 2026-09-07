/* The training-set page: four trays, each ONE server query that matches what
 * the trainer reads (a tray that only reshaped the loaded page would lie about
 * a 10,000-row set), and a correction that writes a HUMAN label — which the
 * store then protects from every later machine pass. */

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
    machine_positive: 1113, human_positive: 18, machine_negative: 9120, human_negative: 46,
    target: 300, in_set: 300, reserve: 831, in_set_unreviewed: 282, cutoff_available: true },
  { id: 2, label: 'exterier - domovní vchod', positive: 173, negative: 10153,
    excluded: 218, machine_positive: 159, human_positive: 14,
    machine_negative: 10150, human_negative: 3,
    target: 300, in_set: 173, reserve: 0, in_set_unreviewed: 159, cutoff_available: true },
];

const ROWS = [
  { image_id: 11, storage_path: 'img/1/11.jpg', state: 'positive', source: 'machine',
    excluded_reason: null, updated_at: null, definition_version: 9, definition_stale: false,
    set_rank: 19, in_set: true, note_id: null, note: null },
  { image_id: 12, storage_path: 'img/1/12.jpg', state: 'positive', source: 'human',
    excluded_reason: null, updated_at: null, definition_version: 8, definition_stale: true,
    set_rank: 1, in_set: true, note_id: 36, note: 'front shot, building is the subject' },
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
    data: { rows: ROWS as never, counts: HEADS[0] as never, limit: 60, offset: 0 },
  });
  vi.mocked(api.setNewDedupTagAnnotation).mockResolvedValue({ data: {} } as never);
});

describe('<NewDedupTrainingSet> the four trays', () => {
  it('opens on the head with the most positives, on the positive tray = the set', async () => {
    renderPage();
    await waitFor(() => expect(api.listTrainingSet).toHaveBeenCalledWith(
      { tag_id: 3, state: 'positive', membership: 'set', limit: 50, offset: 0 },
    ));
    expect(await screen.findByTestId('training-tile-11')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /Training · positive/ })).toHaveAttribute('aria-selected', 'true');
  });

  it('the negative tray is the operator’s negatives ONLY — the door the trainer reads', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    await user.click(screen.getByRole('tab', { name: /Training · negative/ }));
    await waitFor(() => expect(lastQuery()).toEqual(
      { tag_id: 3, state: 'negative', source: 'human', limit: 50, offset: 0 },
    ));
  });

  it('the reserve tray is the positives past the cutoff', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    await user.click(screen.getByRole('tab', { name: /Reserve/ }));
    await waitFor(() => expect(lastQuery()).toEqual(
      { tag_id: 3, state: 'positive', membership: 'reserve', limit: 50, offset: 0 },
    ));
  });

  it('the left-out tray is the excluded labels, whoever wrote them', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    await user.click(screen.getByRole('tab', { name: /Left out/ }));
    await waitFor(() => expect(lastQuery()).toEqual(
      { tag_id: 3, state: 'excluded', limit: 50, offset: 0 },
    ));
  });

  it('each tray carries the size of what it holds, in its own terms', async () => {
    renderPage();
    await screen.findByTestId('training-tile-11');
    expect(screen.getByRole('tab', { name: /Training · positive/ })).toHaveTextContent('300 / 300');
    // 9166 negatives exist, 46 are the operator's: the tray says 46.
    expect(screen.getByRole('tab', { name: /Training · negative/ })).toHaveTextContent('46');
    expect(screen.getByRole('tab', { name: /Training · negative/ })).not.toHaveTextContent('9166');
    expect(screen.getByRole('tab', { name: /Reserve/ })).toHaveTextContent('831');
    expect(screen.getByRole('tab', { name: /Left out/ })).toHaveTextContent('247');
  });

  it('says how many machine negatives are in no tray, instead of hiding them', async () => {
    renderPage();
    await screen.findByTestId('training-tile-11');
    expect(screen.getByTestId('machine-negatives')).toHaveTextContent('9120 machine negatives train nothing');
  });

  it('there are no composable filters and no bulk confirm', async () => {
    renderPage();
    await screen.findByTestId('training-tile-11');
    expect(screen.queryByRole('group', { name: 'verdict' })).toBeNull();
    expect(screen.queryByRole('group', { name: 'decided by' })).toBeNull();
    expect(screen.queryByRole('button', { name: /Confirm the other/ })).toBeNull();
    expect(screen.queryByText(/To review/)).toBeNull();
  });

  it('old links with the review presets land on the positive tray', async () => {
    renderPage(['/new-dedup/training-set?set=review&verdict=negative&source=machine']);
    await waitFor(() => expect(lastQuery()).toEqual(
      { tag_id: 3, state: 'positive', membership: 'set', limit: 50, offset: 0 },
    ));
  });

  it('switches head and resets the page offset', async () => {
    const user = userEvent.setup();
    renderPage(['/new-dedup/training-set?offset=120']);
    await screen.findByTestId('training-tile-11');
    await user.selectOptions(screen.getByLabelText('head'), '2');
    await waitFor(() => expect(lastQuery()).toEqual(
      expect.objectContaining({ tag_id: 2, offset: 0 }),
    ));
  });

  it('says how much of a head is the machine and how much is yours', async () => {
    renderPage();
    expect(await screen.findByText(/18 yours, 1113 the machine/)).toBeInTheDocument();
  });

  it('states that the holdout is deliberately absent', async () => {
    renderPage();
    expect(await screen.findByText(/sealed exam images are excluded/)).toBeInTheDocument();
  });
});

describe('<NewDedupTrainingSet> moving a photo', () => {
  it('a correction writes a human label for THIS head and image', async () => {
    const user = userEvent.setup();
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    await user.click(within(tile).getByRole('button', { name: /^negative 11$/ }));
    await waitFor(() => expect(api.setNewDedupTagAnnotation)
      .toHaveBeenCalledWith(3, 11, 'negative', null));
  });

  it('a leave-out correction carries the pruned reason, never a bare excluded', async () => {
    const user = userEvent.setup();
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    await user.click(within(tile).getByRole('button', { name: /^excluded 11$/ }));
    await waitFor(() => expect(api.setNewDedupTagAnnotation)
      .toHaveBeenCalledWith(3, 11, 'excluded', 'pruned'));
  });

  it('a corrected tile stays in its tray, patched, until the next load', async () => {
    const user = userEvent.setup();
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    expect(api.listTrainingSet).toHaveBeenCalledTimes(1);
    await user.click(within(tile).getByRole('button', { name: /^negative 11$/ }));
    await waitFor(() => expect(api.setNewDedupTagAnnotation).toHaveBeenCalledTimes(1));
    const after = screen.getByTestId('training-tile-11');
    expect(after).toHaveAttribute('data-state', 'negative');
    expect(within(after).getByText('yours')).toBeInTheDocument();
    expect(within(after).getByText(/Now under/)).toHaveTextContent('Training · negative');
    expect(screen.getByTestId('note-form-11')).toBeInTheDocument();
    expect(api.listTrainingSet).toHaveBeenCalledTimes(1);
  });

  it('the pressed applies button on a machine tile is a confirm', async () => {
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    expect(within(tile).getByRole('button', { name: /^positive 11$/ }))
      .toHaveAttribute('title', expect.stringMatching(/Confirm/));
    const yours = screen.getByTestId('training-tile-12');
    expect(within(yours).getByRole('button', { name: /^positive 12$/ }))
      .toHaveAttribute('title', 'Applies');
  });

  it('names who decided each tile, and flags since-replaced wording', async () => {
    renderPage();
    const machine = await screen.findByTestId('training-tile-11');
    expect(within(machine).getByText('machine')).toBeInTheDocument();
    expect(within(machine).queryByText(/old wording/)).toBeNull();
    const stale = screen.getByTestId('training-tile-12');
    expect(within(stale).getByText('yours')).toBeInTheDocument();
    expect(within(stale).getByText(/old wording/)).toBeInTheDocument();
  });

  it('each positive tile says its position and which side of the cutoff it is on', async () => {
    vi.mocked(api.listTrainingSet).mockResolvedValue({
      data: {
        rows: [ROWS[0], { ...ROWS[0], image_id: 13, set_rank: 512, in_set: false }] as never,
        counts: HEADS[0] as never, limit: 60, offset: 0,
      },
    });
    renderPage();
    expect(await screen.findByTestId('membership-11')).toHaveTextContent('in set #19');
    expect(screen.getByTestId('membership-13')).toHaveTextContent('reserve #512');
  });
});

describe('<NewDedupTrainingSet> the target', () => {
  it('changing the target moves the boundary; an empty target restores the default', async () => {
    vi.mocked(api.setTrainingTarget).mockResolvedValue({
      data: { tag_id: 3, target: 200, is_default: false },
    });
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    const input = screen.getByLabelText('target');
    await user.clear(input);
    await user.type(input, '200');
    await user.click(screen.getByRole('button', { name: 'set' }));
    await waitFor(() => expect(api.setTrainingTarget).toHaveBeenCalledWith(3, 200));
    await user.clear(input);
    await user.click(screen.getByRole('button', { name: 'set' }));
    await waitFor(() => expect(api.setTrainingTarget).toHaveBeenLastCalledWith(3, null));
  });

  it('without a cutoff, the positive tray is every positive and the reserve is closed', async () => {
    vi.mocked(api.listTrainingSetHeads).mockResolvedValue({
      data: HEADS.map((h) => ({ ...h, in_set: 0, reserve: 0, in_set_unreviewed: 0,
        cutoff_available: false })) as never,
    });
    vi.mocked(api.listTrainingSet).mockResolvedValue({
      data: { rows: ROWS.map((r) => ({ ...r, set_rank: null, in_set: null })) as never,
        counts: { ...HEADS[0], cutoff_available: false } as never, limit: 60, offset: 0 },
    });
    renderPage();
    await screen.findByTestId('training-tile-11');
    expect(screen.getByTestId('cutoff-unavailable')).toHaveTextContent(/migration 474/);
    expect(screen.getByRole('tab', { name: /Reserve/ })).toBeDisabled();
    expect(screen.getByRole('tab', { name: /Training · positive/ })).toHaveTextContent('1131');
    expect(screen.getByRole('tab', { name: /Training · positive/ })).not.toHaveTextContent('/');
    expect(screen.queryByTestId('membership-11')).toBeNull();
    expect(screen.queryByLabelText('target')).toBeNull();
    expect(lastQuery()).toEqual({ tag_id: 3, state: 'positive', limit: 50, offset: 0 });
  });
});

describe('<NewDedupTrainingSet> reading the page', () => {
  it('shows the whole photo, never a crop, and opens it full-size', async () => {
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    const img = within(tile).getByRole('img');
    expect(img.className).toContain('object-contain');
    expect(img.className).not.toContain('object-cover');
    expect(img.closest('a')).toHaveAttribute('target', '_blank');
  });

  it('labels the tile buttons in words', async () => {
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    expect(within(tile).getByRole('button', { name: /^positive 11$/ })).toHaveTextContent('applies');
    expect(within(tile).getByRole('button', { name: /^negative 11$/ })).toHaveTextContent('no');
    expect(within(tile).getByRole('button', { name: /^excluded 11$/ })).toHaveTextContent('left out');
  });

  it('names heads in full in the picker', async () => {
    renderPage();
    await screen.findByTestId('training-tile-11');
    expect(screen.getByRole('option', { name: /exterier - domovní vchod · 173/ })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /exterier - fasáda · 1131/ })).toBeInTheDocument();
  });

  it('renders typographic characters, not their escape codes', async () => {
    renderPage();
    await screen.findByTestId('training-tile-11');
    expect(document.body.textContent).not.toMatch(/\\u20/);
  });

  it('says so when a tray is empty, and why for the negative tray', async () => {
    vi.mocked(api.listTrainingSet).mockResolvedValue({
      data: { rows: [], counts: HEADS[1] as never, limit: 60, offset: 0 },
    });
    renderPage(['/new-dedup/training-set?set=negative']);
    expect(await screen.findByText(/Nothing in/)).toHaveTextContent('Training · negative');
    expect(screen.getByText(/the machine’s do not train/)).toBeInTheDocument();
  });
});

describe('<NewDedupTrainingSet> paging', () => {
  it('pages forward, and stops when the page is short', async () => {
    renderPage();
    await screen.findByTestId('training-tile-11');
    expect(screen.getByRole('button', { name: /next/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: /previous/ })).toBeDisabled();
  });

  it('offers 50 / 100 / 500 / 2000, sends the choice as the limit, and resets the offset', async () => {
    const user = userEvent.setup();
    renderPage(['/new-dedup/training-set?offset=100']);
    await screen.findByTestId('training-tile-11');
    const group = screen.getByRole('group', { name: 'per page' });
    for (const n of ['50', '100', '500', '2000']) {
      expect(within(group).getByRole('button', { name: n })).toBeInTheDocument();
    }
    await user.click(within(group).getByRole('button', { name: '2000' }));
    await waitFor(() => expect(lastQuery()).toEqual(
      expect.objectContaining({ limit: 2000, offset: 0 }),
    ));
  });

  it('steps by the chosen page size', async () => {
    vi.mocked(api.listTrainingSet).mockResolvedValue({
      data: {
        rows: Array.from({ length: 100 }, (_, i) => ({ ...ROWS[0], image_id: 1000 + i })) as never,
        counts: HEADS[0] as never, limit: 100, offset: 0,
      },
    });
    const user = userEvent.setup();
    renderPage(['/new-dedup/training-set?n=100']);
    await screen.findByTestId('training-tile-1000');
    await user.click(screen.getByRole('button', { name: /next/ }));
    await waitFor(() => expect(lastQuery()).toEqual(
      expect.objectContaining({ limit: 100, offset: 100 }),
    ));
  });

  it('states the tray total and jumps to the last page, where reserve arrivals land', async () => {
    const user = userEvent.setup();
    renderPage(['/new-dedup/training-set?set=reserve']);
    await screen.findByTestId('training-tile-11');
    expect(screen.getByTestId('page-range')).toHaveTextContent('1–2 of 831');
    await user.click(screen.getByTestId('jump-last'));
    // 831 rows at 50 per page: the last page starts at 800.
    await waitFor(() => expect(lastQuery()).toEqual(
      expect.objectContaining({ offset: 800, limit: 50 }),
    ));
  });
});

describe('<NewDedupTrainingSet> the reason for a change', () => {
  it('offers a note field only on a tile whose mark changed, and sends it with from/to', async () => {
    const user = userEvent.setup();
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    expect(screen.queryByTestId('note-form-11')).toBeNull();
    await user.click(within(tile).getByRole('button', { name: /^negative 11$/ }));
    await waitFor(() => expect(api.setNewDedupTagAnnotation).toHaveBeenCalledTimes(1));
    const form = await screen.findByTestId('note-form-11');
    expect(screen.queryByTestId('note-form-12')).toBeNull();
    await user.type(within(form).getByRole('textbox'), 'entrance door, facade is only the backdrop');
    await user.click(within(form).getByRole('button', { name: 'save' }));
    await waitFor(() => expect(api.setNewDedupTagAnnotation).toHaveBeenLastCalledWith(
      3, 11, 'negative', null,
      { text: 'entrance door, facade is only the backdrop', from_state: 'positive' },
    ));
  });

  it('will not save an empty note', async () => {
    const user = userEvent.setup();
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    await user.click(within(tile).getByRole('button', { name: /^excluded 11$/ }));
    const form = await screen.findByTestId('note-form-11');
    expect(within(form).getByRole('button', { name: 'save' })).toBeDisabled();
    await user.type(within(form).getByRole('textbox'), '   ');
    expect(within(form).getByRole('button', { name: 'save' })).toBeDisabled();
  });

  it('shows a saved note on its photo, with edit and remove', async () => {
    renderPage();
    const tile = await screen.findByTestId('training-tile-12');
    expect(within(tile).getByTestId('note-saved-12'))
      .toHaveTextContent('front shot, building is the subject');
    expect(screen.getByTestId('note-add-11')).toBeInTheDocument();
    expect(screen.queryByTestId('note-add-12')).toBeNull();
  });

  it('editing sends a PATCH with the new text and shows it in place', async () => {
    vi.mocked(api.editTagLabelNote).mockResolvedValue({
      data: { id: 36, image_id: 12, note: 'must be a frontal shot' } as never,
    });
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-12');
    await user.click(screen.getByRole('button', { name: 'edit note' }));
    const input = screen.getByLabelText('why 12');
    expect(input).toHaveValue('front shot, building is the subject');
    await user.clear(input);
    await user.type(input, 'must be a frontal shot');
    await user.click(within(screen.getByTestId('note-form-12')).getByRole('button', { name: 'save' }));
    await waitFor(() => expect(api.editTagLabelNote)
      .toHaveBeenCalledWith(36, 'must be a frontal shot'));
    await waitFor(() => expect(screen.getByTestId('note-saved-12'))
      .toHaveTextContent('must be a frontal shot'));
    expect(api.setNewDedupTagAnnotation).not.toHaveBeenCalled();
  });

  it('removing drops the note and offers to add one again', async () => {
    vi.mocked(api.deleteTagLabelNote).mockResolvedValue({
      data: { id: 36, image_id: 12, tag_id: 3 },
    });
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-12');
    await user.click(screen.getByRole('button', { name: 'remove' }));
    await waitFor(() => expect(api.deleteTagLabelNote).toHaveBeenCalledWith(36));
    await waitFor(() => expect(screen.queryByTestId('note-saved-12')).toBeNull());
    expect(screen.getByTestId('note-add-12')).toBeInTheDocument();
  });

  it('a note added without a mark change re-states the current mark, from null', async () => {
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
