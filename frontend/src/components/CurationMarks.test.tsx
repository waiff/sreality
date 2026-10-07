/* MS16: a merge hides nothing it will touch. The merge bar names every ticked
 * property with its marks and how many "Různé" rulings the merge would take back
 * (MS12), the note mark counts the caller's notes, and a failed read is a retry,
 * never an absent mark or a count of none. */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { MergeModeBar } from './BrowseExperience';
import { MergeSelection } from './CurationMarks';
import NoteMark from './NoteMark';
import * as api from '@/lib/api';
import * as queries from '@/lib/queries';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  getMergePreview: vi.fn(),
}));

vi.mock('@/lib/queries', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/queries')>()),
  fetchPipelineMembers: vi.fn(),
  fetchPipelineStages: vi.fn(async () => []),
  fetchPropertyCollectionMemberSet: vi.fn(),
  fetchIsDismissed: vi.fn(),
  fetchNoteCounts: vi.fn(),
}));

const renderWith = (ui: React.ReactElement) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      {ui}
    </QueryClientProvider>,
  );

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(queries.fetchPipelineMembers).mockResolvedValue(new Map([[42, {
    property_id: 42, stage_id: 1, stage_label: 'Prověřit', stage_color: 'copper',
    stage_code: '1', stage_position: 1, is_terminal: false,
  }]]));
  vi.mocked(queries.fetchPropertyCollectionMemberSet).mockResolvedValue(new Map([[43, [7]]]));
  vi.mocked(queries.fetchIsDismissed).mockImplementation(async (id) => id === 43);
  vi.mocked(queries.fetchNoteCounts).mockResolvedValue(new Map([[42, 2]]));
  vi.mocked(api.getMergePreview).mockResolvedValue({ property_ids: [42, 43], rulings_taken_back: 0 });
});

const mergeBar = (ids: number[]) => (
  <MergeModeBar active selected={new Set(ids)} merging={false} onToggle={() => {}} onMerge={() => {}} />
);

describe('<MergeSelection> in the merge bar', () => {
  it('asks for a pick while nothing is ticked', () => {
    renderWith(mergeBar([]));
    expect(screen.getByText('Pick listings to merge')).toBeInTheDocument();
  });

  it('lists every ticked property with the marks a merge will carry, read-only', async () => {
    renderWith(mergeBar([42, 43]));
    const list = screen.getByRole('list', { name: 'Vybráno ke sloučení' });
    const [first, second] = within(list).getAllByRole('listitem');
    expect(first).toHaveTextContent('#42');
    expect(await within(first).findByText('V pipeline: Prověřit')).toBeInTheDocument();
    expect(await within(first).findByText('Poznámky: 2')).toBeInTheDocument();
    expect(second).toHaveTextContent('#43');
    expect(await within(second).findByText('V kolekci')).toBeInTheDocument();
    expect(await within(second).findByText('Skryto')).toBeInTheDocument();
    expect(within(list).queryByRole('button')).toBeNull();
  });

  it('says how many "Různé" rulings the merge would take back, once two are ticked', async () => {
    vi.mocked(api.getMergePreview).mockResolvedValue({ property_ids: [42, 43], rulings_taken_back: 3 });
    renderWith(mergeBar([42]));
    expect(api.getMergePreview).not.toHaveBeenCalled();
    renderWith(mergeBar([43, 42]));
    expect(await screen.findByText('Vezme zpět 3 rozhodnutí „Různé“.')).toBeInTheDocument();
    expect(api.getMergePreview).toHaveBeenCalledWith([42, 43]);
  });

  it('says nothing of rulings when the merge takes none back', async () => {
    renderWith(mergeBar([42, 43]));
    await waitFor(() => expect(api.getMergePreview).toHaveBeenCalled());
    expect(screen.queryByText(/Vezme zpět/)).toBeNull();
  });

  it('holds the merge until the count is read', async () => {
    let answer: (p: api.MergePreview) => void = () => {};
    vi.mocked(api.getMergePreview).mockReturnValue(new Promise((resolve) => (answer = resolve)));
    renderWith(mergeBar([42, 43]));
    expect(
      await screen.findByText('Zjišťuji, kolik rozhodnutí „Různé“ sloučení vezme zpět…'),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Merge 2' })).toBeDisabled();
    await act(async () => answer({ property_ids: [42, 43], rulings_taken_back: 1 }));
    expect(await screen.findByText('Vezme zpět 1 rozhodnutí „Různé“.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Merge 2' })).toBeEnabled();
  });

  it('a count it could not read is said, with a retry, never shown as none', async () => {
    vi.mocked(api.getMergePreview)
      .mockRejectedValueOnce(new Error('HTTP 500'))
      .mockResolvedValue({ property_ids: [42, 43], rulings_taken_back: 2 });
    renderWith(mergeBar([42, 43]));
    expect(
      await screen.findByText(/Kolik rozhodnutí „Různé“ sloučení vezme zpět, se nepodařilo zjistit./),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Merge 2' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Zkusit znovu' }));
    expect(await screen.findByText('Vezme zpět 2 rozhodnutí „Různé“.')).toBeInTheDocument();
  });

  it('turns a failed read into a retry instead of dropping the mark', async () => {
    vi.mocked(queries.fetchPipelineMembers).mockRejectedValueOnce(new Error('HTTP 500'));
    vi.mocked(queries.fetchPropertyCollectionMemberSet).mockRejectedValueOnce(new Error('HTTP 500'));
    renderWith(<MergeSelection ids={new Set([43])} />);
    fireEvent.click(await screen.findByRole('button', { name: /Kolekce se nepodařilo načíst/ }));
    expect(await screen.findByText('V kolekci')).toBeInTheDocument();
    vi.mocked(queries.fetchPipelineMembers).mockResolvedValue(new Map([[43, {
      property_id: 43, stage_id: 1, stage_label: 'Prověřit', stage_color: 'copper',
      stage_code: '1', stage_position: 1, is_terminal: false,
    }]]));
    fireEvent.click(screen.getByRole('button', { name: /Pipeline se nepodařilo načíst/ }));
    expect(await screen.findByText('V pipeline: Prověřit')).toBeInTheDocument();
  });
});

describe('<NoteMark>', () => {
  it('counts the caller’s notes, and shows nothing for a property without any', async () => {
    renderWith(<NoteMark property_id={42} />);
    expect(await screen.findByTitle('Poznámky: 2')).toHaveTextContent('2');
    const { container } = renderWith(<NoteMark property_id={43} />);
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it('offers a retry when the counts cannot be read, and reads again', async () => {
    vi.mocked(queries.fetchNoteCounts).mockRejectedValueOnce(new Error('HTTP 500'));
    renderWith(<NoteMark property_id={42} />);
    fireEvent.click(await screen.findByRole('button', { name: /Poznámky se nepodařilo načíst/ }));
    expect(await screen.findByTitle('Poznámky: 2')).toBeInTheDocument();
  });
});
