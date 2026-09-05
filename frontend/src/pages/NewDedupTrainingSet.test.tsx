/* The training-set review surface. What matters: the filters actually reach
 * the server (a filter that only reshapes what is already loaded would lie
 * about a 10,000-row set), and a correction writes a HUMAN label — which the
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
    machine_positive: 1113, human_positive: 18,
    target: 300, in_set: 300, reserve: 831, in_set_unreviewed: 282 },
  { id: 2, label: 'exterier - domovní vchod', positive: 173, negative: 10153,
    excluded: 218, machine_positive: 159, human_positive: 14,
    target: 300, in_set: 173, reserve: 0, in_set_unreviewed: 159 },
];

const ROWS = [
  { image_id: 11, storage_path: 'img/1/11.jpg', state: 'positive', source: 'machine',
    excluded_reason: null, updated_at: null, definition_version: 9, definition_stale: false,
    set_rank: 19, in_set: true },
  { image_id: 12, storage_path: 'img/1/12.jpg', state: 'positive', source: 'human',
    excluded_reason: null, updated_at: null, definition_version: 8, definition_stale: true,
    set_rank: 1, in_set: true },
];

function renderPage(entries = ['/new-dedup/training-set']) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={entries}><NewDedupTrainingSet /></MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.listTrainingSetHeads).mockResolvedValue({ data: HEADS as never });
  vi.mocked(api.listTrainingSet).mockResolvedValue({
    data: { rows: ROWS as never, counts: HEADS[0] as never, limit: 60, offset: 0 },
  });
  vi.mocked(api.setNewDedupTagAnnotation).mockResolvedValue({ data: {} } as never);
});

describe('<NewDedupTrainingSet>', () => {
  it('opens on the head with the most positives, on the bounded review', async () => {
    // 'To review' = in the set + machine + positive, composed from the three
    // server filters so it cannot disagree with them.
    renderPage();
    await waitFor(() => expect(api.listTrainingSet).toHaveBeenCalledWith(
      expect.objectContaining({
        tag_id: 3, state: 'positive', source: 'machine', membership: 'set', limit: 60, offset: 0,
      }),
    ));
    expect(await screen.findByTestId('training-tile-11')).toBeInTheDocument();
  });

  it('says how much of a head is the machine and how much is yours', async () => {
    renderPage();
    expect(await screen.findByText(/1113 by the machine, 18 yours/)).toBeInTheDocument();
  });

  it('filters by verdict AT THE SERVER, not in the loaded page', async () => {
    const user = userEvent.setup();
    renderPage(['/new-dedup/training-set?set=all']);
    await screen.findByTestId('training-tile-11');
    await user.click(screen.getByRole('button', { name: /Does not/ }));
    await waitFor(() => expect(api.listTrainingSet).toHaveBeenLastCalledWith(
      expect.objectContaining({ tag_id: 3, state: 'negative' }),
    ));
  });

  it('filters by who decided, and drops the filter for "anyone"', async () => {
    const user = userEvent.setup();
    renderPage(['/new-dedup/training-set?set=all']);
    await screen.findByTestId('training-tile-11');
    await user.click(screen.getByRole('button', { name: 'Yours' }));
    await waitFor(() => expect(api.listTrainingSet).toHaveBeenLastCalledWith(
      expect.objectContaining({ source: 'human' }),
    ));
    await user.click(screen.getByRole('button', { name: 'Anyone' }));
    await waitFor(() => {
      const last = vi.mocked(api.listTrainingSet).mock.calls.at(-1)?.[0];
      expect(last).not.toHaveProperty('source');
    });
  });

  it('switches head and resets the page offset', async () => {
    const user = userEvent.setup();
    renderPage(['/new-dedup/training-set?offset=120']);
    await screen.findByTestId('training-tile-11');
    await user.selectOptions(screen.getByLabelText('head'), '2');
    await waitFor(() => expect(api.listTrainingSet).toHaveBeenLastCalledWith(
      expect.objectContaining({ tag_id: 2, offset: 0 }),
    ));
  });

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

  it('marks a label written under wording that has since been replaced', async () => {
    renderPage();
    const stale = await screen.findByTestId('training-tile-12');
    expect(within(stale).getByText(/old wording/)).toBeInTheDocument();
    const current = screen.getByTestId('training-tile-11');
    expect(within(current).queryByText(/old wording/)).toBeNull();
  });

  it('names who decided each tile', async () => {
    renderPage();
    const machine = await screen.findByTestId('training-tile-11');
    expect(within(machine).getByText('machine')).toBeInTheDocument();
    expect(within(screen.getByTestId('training-tile-12')).getByText('yours')).toBeInTheDocument();
  });

  it('states that the holdout is deliberately absent', async () => {
    renderPage();
    expect(await screen.findByText(/sealed exam images are excluded/)).toBeInTheDocument();
  });

  it('pages forward, and stops when the page is short', async () => {
    renderPage();
    await screen.findByTestId('training-tile-11');
    // Two rows against a 60-row page means there is no next page.
    expect(screen.getByRole('button', { name: /next/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: /previous/ })).toBeDisabled();
  });

  it('says so when a head has nothing under the current filter', async () => {
    vi.mocked(api.listTrainingSet).mockResolvedValue({
      data: { rows: [], counts: HEADS[1] as never, limit: 60, offset: 0 },
    });
    renderPage();
    expect(await screen.findByText(/Nothing labeled for this head/)).toBeInTheDocument();
  });
});

describe('<NewDedupTrainingSet> the reason for a change', () => {
  it('offers a note field only on a tile whose mark changed, and sends it with from/to', async () => {
    const user = userEvent.setup();
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    // No field before any change.
    expect(screen.queryByTestId('note-form-11')).toBeNull();
    await user.click(within(tile).getByRole('button', { name: /^negative 11$/ }));
    await waitFor(() => expect(api.setNewDedupTagAnnotation).toHaveBeenCalledTimes(1));
    // The change came from a machine "positive"; the field appears on THIS tile only.
    const form = await screen.findByTestId('note-form-11');
    expect(screen.queryByTestId('note-form-12')).toBeNull();
    await user.type(within(form).getByRole('textbox'), 'entrance door, facade is only the backdrop');
    await user.click(within(form).getByRole('button', { name: 'save' }));
    // The note re-states the SAME mark with the reason attached — one write
    // path for mark and reason — and carries what the tile showed before.
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
});


describe('<NewDedupTrainingSet> the cutoff', () => {
  it('shows the bounded review count, the set against its target, and the reserve', async () => {
    renderPage();
    await screen.findByTestId('training-tile-11');
    expect(screen.getByRole('button', { name: /To review/ })).toHaveTextContent('282');
    expect(screen.getByRole('button', { name: /In set/ })).toHaveTextContent('300/300');
    expect(screen.getByRole('button', { name: /Reserve/ })).toHaveTextContent('831');
  });

  it('the reserve view asks the server for positives past the cutoff', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    await user.click(screen.getByRole('button', { name: /Reserve/ }));
    await waitFor(() => expect(api.listTrainingSet).toHaveBeenLastCalledWith(
      expect.objectContaining({ membership: 'reserve' }),
    ));
  });

  it('each positive tile says whether it is in the set', async () => {
    vi.mocked(api.listTrainingSet).mockResolvedValue({
      data: {
        rows: [
          ROWS[0],
          { ...ROWS[0], image_id: 13, set_rank: 512, in_set: false },
        ] as never,
        counts: HEADS[0] as never, limit: 60, offset: 0,
      },
    });
    renderPage(['/new-dedup/training-set?set=all']);
    expect(await screen.findByTestId('membership-11')).toHaveTextContent('in set');
    expect(screen.getByTestId('membership-13')).toHaveTextContent('reserve');
  });

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

  it('the pressed applies button on a machine tile is a confirm', async () => {
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    expect(within(tile).getByRole('button', { name: /^positive 11$/ }))
      .toHaveAttribute('title', expect.stringMatching(/Confirm/));
    const yours = screen.getByTestId('training-tile-12');
    expect(within(yours).getByRole('button', { name: /^positive 12$/ }))
      .toHaveAttribute('title', 'Applies');
  });
});

describe('<NewDedupTrainingSet> the family is part of the tag', () => {
  it('names heads in full in the picker', async () => {
    renderPage();
    await screen.findByTestId('training-tile-11');
    expect(screen.getByRole('option', { name: /exterier - domovní vchod · 173/ })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /exterier - fasáda · 1131/ })).toBeInTheDocument();
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

  it('labels the tile buttons in words, and explains the page on demand', async () => {
    renderPage();
    const tile = await screen.findByTestId('training-tile-11');
    expect(within(tile).getByRole('button', { name: /^positive 11$/ })).toHaveTextContent('applies');
    expect(within(tile).getByRole('button', { name: /^negative 11$/ })).toHaveTextContent('no');
    expect(within(tile).getByRole('button', { name: /^excluded 11$/ })).toHaveTextContent('left out');
    expect(screen.getByText('How to use this page')).toBeInTheDocument();
    expect(screen.getByText(/the first reserve photo steps in automatically/)).toBeInTheDocument();
  });

  it('renders typographic characters, not their escape codes', async () => {
    renderPage();
    await screen.findByTestId('training-tile-11');
    expect(document.body.textContent).not.toMatch(/\\u20/);
  });
});

describe('<NewDedupTrainingSet> a corrected tile stays put', () => {
  it('does not refetch the list after a correction, so the note field survives', async () => {
    // The operator clicked "no" on a positive under the Applies filter; the
    // list refetched, the row no longer matched, and the tile vanished with
    // the note field on it. The list is patched in place instead.
    const user = userEvent.setup();
    renderPage(['/new-dedup/training-set?set=all']);
    const tile = await screen.findByTestId('training-tile-11');
    expect(api.listTrainingSet).toHaveBeenCalledTimes(1);
    await user.click(within(tile).getByRole('button', { name: /^negative 11$/ }));
    await waitFor(() => expect(api.setNewDedupTagAnnotation).toHaveBeenCalledTimes(1));
    // Still here, now a human negative, note field present, list not refetched.
    const after = screen.getByTestId('training-tile-11');
    expect(after).toHaveAttribute('data-state', 'negative');
    expect(within(after).getByText('yours')).toBeInTheDocument();
    expect(screen.getByTestId('note-form-11')).toBeInTheDocument();
    expect(api.listTrainingSet).toHaveBeenCalledTimes(1);
    // And it says where the photo now lives.
    expect(within(after).getByText(/now under/i)).toBeInTheDocument();
  });
});

describe('<NewDedupTrainingSet> the filter groups', () => {
  it('names the three groups and locks two of them under "To review"', async () => {
    renderPage();
    await screen.findByTestId('training-tile-11');
    expect(screen.getByRole('group', { name: 'cutoff' })).toBeInTheDocument();
    expect(screen.getByRole('group', { name: 'verdict' })).toBeInTheDocument();
    expect(screen.getByRole('group', { name: 'decided by' })).toBeInTheDocument();
    // "To review" fixes verdict=applies and decided-by=machine; the other two
    // groups are visibly locked rather than silently ignored.
    expect(screen.getByRole('button', { name: /Does not/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Yours' })).toBeDisabled();
    expect(screen.getAllByText(/set by “To review”/i).length).toBeGreaterThan(0);
  });

  it('unlocks them under any other cutoff', async () => {
    renderPage(['/new-dedup/training-set?set=all']);
    await screen.findByTestId('training-tile-11');
    expect(screen.getByRole('button', { name: /Does not/ })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Yours' })).toBeEnabled();
  });
});
