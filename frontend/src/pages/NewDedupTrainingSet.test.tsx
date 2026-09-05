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
    machine_positive: 1113, human_positive: 18 },
  { id: 2, label: 'exterier - domovní vchod', positive: 173, negative: 10153,
    excluded: 218, machine_positive: 159, human_positive: 14 },
];

const ROWS = [
  { image_id: 11, storage_path: 'img/1/11.jpg', state: 'positive', source: 'machine',
    excluded_reason: null, updated_at: null, definition_version: 9, definition_stale: false },
  { image_id: 12, storage_path: 'img/1/12.jpg', state: 'positive', source: 'human',
    excluded_reason: null, updated_at: null, definition_version: 8, definition_stale: true },
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
  it('opens on the head with the most positives and asks for its applies', async () => {
    renderPage();
    await waitFor(() => expect(api.listTrainingSet).toHaveBeenCalledWith(
      expect.objectContaining({ tag_id: 3, state: 'positive', limit: 60, offset: 0 }),
    ));
    expect(await screen.findByTestId('training-tile-11')).toBeInTheDocument();
  });

  it('says how much of a head is the machine and how much is yours', async () => {
    renderPage();
    expect(await screen.findByText(/1113 by the machine, 18 yours/)).toBeInTheDocument();
  });

  it('filters by verdict AT THE SERVER, not in the loaded page', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId('training-tile-11');
    await user.click(screen.getByRole('button', { name: /Does not/ }));
    await waitFor(() => expect(api.listTrainingSet).toHaveBeenLastCalledWith(
      expect.objectContaining({ tag_id: 3, state: 'negative' }),
    ));
  });

  it('filters by who decided, and drops the filter for "anyone"', async () => {
    const user = userEvent.setup();
    renderPage();
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
