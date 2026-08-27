/* NewDedupTaxonomy — the tag-definitions workbench (migration 445,
 * docs/design/tag-annotation-matrix.md).
 *
 * Hermetic: every api.ts call and the Supabase image read are mocked. What is
 * pinned here is the ONE behaviour the data model forces on the UI — there are
 * no drafts server-side, so a whole sitting of edits (text, gallery clicks,
 * "add to confusable") must batch into exactly ONE saveTagDefinition call. A
 * test that lets any of those write on their own would be pinning a bug.
 *
 * Also pinned: a definition points at other tags BY ID, so the save payload
 * carries tag_ids and never label text.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import NewDedupTaxonomy from './NewDedupTaxonomy';
import type {
  NewDedupLabelingOverview,
  NewDedupTag,
  TagDefinition,
  TagDefinitionStatus,
  TagDefinitionVersion,
  TagNeighbour,
  TagPositiveImage,
} from '@/lib/api';
import * as api from '@/lib/api';
import * as queries from '@/lib/queries';
import type { ImagePublic } from '@/lib/types';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    getNewDedupLabelingOverview: vi.fn(),
    listTagDefinitionStatus: vi.fn(),
    getTagDefinition: vi.fn(),
    saveTagDefinition: vi.fn(),
    listTagDefinitionVersions: vi.fn(),
    getTagDefinitionVersion: vi.fn(),
    listTagPositiveImages: vi.fn(),
    listTagNeighbours: vi.fn(),
  };
});

vi.mock('@/lib/queries', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/queries')>();
  return { ...actual, fetchImagesByImageIds: vi.fn() };
});

function tag(over: Partial<NewDedupTag> = {}): NewDedupTag {
  return {
    id: 1, label: 'interier - kuchyne', family: null, active: true,
    priority: false, ready_for_training: false,
    created_at: '2026-08-01T00:00:00Z',
    positive_count: 12, gate_count: 12, border_case_count: 0,
    negative_count: 0, excluded_count: 0, pending_count: 0, dismissed_count: 0,
    ...over,
  };
}

const KUCHYNE = tag({ id: 1, label: 'interier - kuchyne', priority: true });
const KOUPELNA = tag({ id: 2, label: 'interier - koupelna' });
const FASADA = tag({ id: 3, label: 'exterier - fasada' });

const OVERVIEW: NewDedupLabelingOverview = {
  sample_size: 42,
  tags: [KUCHYNE, KOUPELNA, FASADA],
};

const STATUS: TagDefinitionStatus[] = [
  {
    tag_id: 1, definition_id: 77, version: 2,
    means: 'A room whose primary function is cooking.',
    created_at: '2026-08-20T00:00:00Z',
  },
];

const DEFINITION: TagDefinition = {
  id: 77, tag_id: 1, version: 2,
  means: 'A room whose primary function is cooking.',
  counts: ['fitted kitchen units'],
  does_not_count: [{ case: 'a kitchenette in a studio', goes_to_tag_id: 2 }],
  confusable_with: [{ tag_id: 3, tell: 'units along a wall' }],
  leave_out_when: null,
  example_image_ids: [],
  status: 'active',
  created_at: '2026-08-20T00:00:00Z',
  created_by: 'operator',
  referenced_tags: [
    { tag_id: 2, label: 'interier - koupelna' },
    { tag_id: 3, label: 'exterier - fasada' },
  ],
};

const VERSIONS: TagDefinitionVersion[] = [
  {
    id: 77, version: 2, status: 'active',
    means: 'A room whose primary function is cooking.',
    created_at: '2026-08-20T00:00:00Z', created_by: 'operator',
  },
  {
    id: 70, version: 1, status: 'superseded', means: 'Kitchen.',
    created_at: '2026-08-10T00:00:00Z', created_by: 'operator',
  },
];

const POSITIVES: TagPositiveImage[] = [
  {
    image_id: 101, storage_path: 'img/555/1.jpg',
    sreality_url: 'https://sdn.cz/x.jpg', updated_at: '2026-08-21T00:00:00Z',
  },
];

const NEIGHBOURS: TagNeighbour[] = [
  {
    tag_id: 2, label: 'interier - koupelna', family: null,
    embedded_positive_count: 12, cosine_distance: 0.1234,
  },
];

const IMAGE: ImagePublic = {
  id: 101, sreality_id: 555, sequence: 1, sreality_url: 'https://sdn.cz/x.jpg',
  storage_path: 'img/555/1.jpg', clip_fine_tag: null, clip_logical_tag: null,
  clip_confidence: null, clip_render_score: null, phash: null,
};

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/new-dedup/labeling/taxonomy']}>
        <NewDedupTaxonomy />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/* Selecting a tag is how every test past the first one starts: the right column
 * renders nothing until one is picked. */
async function selectKuchyne() {
  const row = await screen.findByRole('button', { name: /kuchyne/ });
  fireEvent.click(row);
  await screen.findByLabelText('means');
}

const saveBtn = () => screen.getByRole('button', { name: /^Save v/ });

describe('<NewDedupTaxonomy>', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getNewDedupLabelingOverview).mockResolvedValue({ data: OVERVIEW });
    vi.mocked(api.listTagDefinitionStatus).mockResolvedValue({ data: STATUS });
    vi.mocked(api.getTagDefinition).mockResolvedValue({ data: DEFINITION });
    vi.mocked(api.listTagDefinitionVersions).mockResolvedValue({ data: VERSIONS });
    vi.mocked(api.getTagDefinitionVersion).mockResolvedValue({
      data: { ...DEFINITION, id: 70, version: 1, means: 'Kitchen.', status: 'superseded' },
    });
    vi.mocked(api.listTagPositiveImages).mockResolvedValue({ data: POSITIVES });
    vi.mocked(api.listTagNeighbours).mockResolvedValue({ data: NEIGHBOURS });
    vi.mocked(api.saveTagDefinition).mockResolvedValue({
      data: { ...DEFINITION, version: 3 },
    });
    vi.mocked(queries.fetchImagesByImageIds).mockResolvedValue(new Map([[101, IMAGE]]));
  });

  // --- tag list ------------------------------------------------------------

  it('groups tags by the label prefix and shows each one\'s definition status', async () => {
    renderPage();
    await screen.findByText('exterier');
    expect(screen.getByText('interier')).toBeInTheDocument();

    // The one defined tag carries its version; the undefined ones an em dash.
    const kuchyne = screen.getByRole('button', { name: /kuchyne/ });
    expect(within(kuchyne).getByText('v2')).toBeInTheDocument();
    const koupelna = screen.getByRole('button', { name: /koupelna/ });
    expect(within(koupelna).getByText('—')).toBeInTheDocument();

    expect(screen.getByText('3 tags · 1 defined')).toBeInTheDocument();
  });

  it('renders a priority tag in the established brick treatment', async () => {
    renderPage();
    const kuchyne = await screen.findByRole('button', { name: /kuchyne/ });
    expect(within(kuchyne).getByText('kuchyne').className).toContain(
      'text-[var(--color-brick)]',
    );
    const fasada = screen.getByRole('button', { name: /fasada/ });
    expect(within(fasada).getByText('fasada').className).toContain('text-[var(--color-ink-2)]');
  });

  // --- editor --------------------------------------------------------------

  it('loads the selected tag\'s definition into the form', async () => {
    renderPage();
    await selectKuchyne();

    expect(api.getTagDefinition).toHaveBeenCalledWith(1);
    expect(screen.getByLabelText('means')).toHaveValue(
      'A room whose primary function is cooking.',
    );
    expect(screen.getByLabelText('counts 1')).toHaveValue('fitted kitchen units');
    expect(screen.getByLabelText('does not count 1')).toHaveValue('a kitchenette in a studio');
    // Tag references resolve to the picked ID, never to label text.
    expect(screen.getByLabelText('does not count 1 goes to tag')).toHaveValue('2');
    expect(screen.getByLabelText('confusable with 1 tag')).toHaveValue('3');
    expect(screen.getByLabelText('confusable with 1 tell')).toHaveValue('units along a wall');
  });

  it('saves one new version carrying tag_ids, not labels', async () => {
    renderPage();
    await selectKuchyne();

    expect(saveBtn()).toBeDisabled();
    fireEvent.change(screen.getByLabelText('means'), {
      target: { value: 'The room where food is cooked.' },
    });
    expect(saveBtn()).toBeEnabled();
    fireEvent.click(saveBtn());

    await waitFor(() => expect(api.saveTagDefinition).toHaveBeenCalledTimes(1));
    expect(api.saveTagDefinition).toHaveBeenCalledWith(1, {
      means: 'The room where food is cooked.',
      counts: ['fitted kitchen units'],
      does_not_count: [{ case: 'a kitchenette in a studio', goes_to_tag_id: 2 }],
      confusable_with: [{ tag_id: 3, tell: 'units along a wall' }],
      leave_out_when: null,
      example_image_ids: [],
      // The version the form was loaded from — the server refuses the save if
      // another tab has moved the definition on since.
      base_version: 2,
    });
  });

  it('is clean again after a save, and does not offer to write the same version twice', async () => {
    vi.mocked(api.saveTagDefinition).mockResolvedValue({
      data: { ...DEFINITION, id: 78, version: 3, means: 'The room where food is cooked.' },
    });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderPage();
    await selectKuchyne();

    fireEvent.change(screen.getByLabelText('means'), {
      target: { value: 'The room where food is cooked.' },
    });
    fireEvent.click(saveBtn());
    await waitFor(() => expect(api.saveTagDefinition).toHaveBeenCalledTimes(1));

    // The saved document IS the new baseline: nothing is unsaved, so a second
    // click cannot write a byte-identical v4, and the next tag switch does not
    // ask to discard work that is already stored.
    await waitFor(() => expect(saveBtn()).toBeDisabled());
    expect(screen.queryByText(/unsaved/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Discard changes' })).not.toBeInTheDocument();
    expect(saveBtn()).toHaveTextContent('Save v4');

    fireEvent.click(screen.getByRole('button', { name: /koupelna/ }));
    expect(confirm).not.toHaveBeenCalled();
    confirm.mockRestore();
  });

  it('sends base_version null for a tag that had no definition when it loaded', async () => {
    vi.mocked(api.getTagDefinition).mockResolvedValue({ data: null });
    vi.mocked(api.listTagDefinitionVersions).mockResolvedValue({ data: [] });
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: /koupelna/ }));
    await screen.findByLabelText('means');

    fireEvent.change(screen.getByLabelText('means'), { target: { value: 'A bathroom.' } });
    fireEvent.click(saveBtn());
    await waitFor(() => expect(api.saveTagDefinition).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.saveTagDefinition).mock.calls[0][1].base_version).toBeNull();
  });

  it('will not save a does-not-count row that names a tag but no case', async () => {
    renderPage();
    await selectKuchyne();

    fireEvent.change(screen.getByLabelText('does not count 1'), { target: { value: '' } });
    expect(
      screen.getByText(/Needs the case this tag takes instead/),
    ).toBeInTheDocument();
    // Half-written, not empty: toPayload would drop it silently, so Save blocks.
    expect(saveBtn()).toBeDisabled();

    fireEvent.change(screen.getByLabelText('does not count 1 goes to tag'), {
      target: { value: '' },
    });
    expect(saveBtn()).toBeEnabled();
  });

  it('never drops unsaved writing on a tag switch without asking', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderPage();
    await selectKuchyne();
    fireEvent.change(screen.getByLabelText('means'), { target: { value: 'half a sentence' } });

    fireEvent.click(screen.getByRole('button', { name: /koupelna/ }));
    expect(confirm).toHaveBeenCalledTimes(1);
    // Declined — still on kuchyne, still holding the unsaved text.
    expect(screen.getByLabelText('means')).toHaveValue('half a sentence');
    expect(api.getTagDefinition).toHaveBeenCalledTimes(1);

    confirm.mockReturnValue(true);
    fireEvent.click(screen.getByRole('button', { name: /koupelna/ }));
    await waitFor(() => expect(api.getTagDefinition).toHaveBeenCalledWith(2));
    confirm.mockRestore();
  });

  it('cannot list the subject tag as confusable with itself', async () => {
    renderPage();
    await selectKuchyne();

    const picker = screen.getByLabelText('confusable with 1 tag');
    const offered = within(picker).getAllByRole('option').map((o) => o.textContent);
    expect(offered).toContain('fasada');
    expect(offered).not.toContain('kuchyne');
  });

  it('renders a past version read-only, with no way to save from it', async () => {
    renderPage();
    await selectKuchyne();

    fireEvent.change(screen.getByLabelText('Definition history'), { target: { value: '1' } });
    await waitFor(() => expect(api.getTagDefinitionVersion).toHaveBeenCalledWith(1, 1));
    expect(await screen.findByText(/Viewing v1 \(superseded\) — read only/)).toBeInTheDocument();
    // The old body, and its tag references resolved through the version's own
    // referenced_tags rather than through today's taxonomy.
    expect(await screen.findByText('Kitchen.')).toBeInTheDocument();
    expect(screen.getByText('exterier - fasada')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Save v/ })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('means')).not.toBeInTheDocument();
  });

  // --- what this tag actually contains -------------------------------------

  it('stages an example image without writing, then saves it with the definition', async () => {
    renderPage();
    await selectKuchyne();
    const tile = await screen.findByRole('button', {
      name: 'Toggle image 101 as a canonical example',
    });

    fireEvent.click(tile);
    expect(tile).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText(/1 marked as examples/)).toBeInTheDocument();
    // The whole point of the batching rule: a toggle is NOT a write.
    expect(api.saveTagDefinition).not.toHaveBeenCalled();

    fireEvent.click(saveBtn());
    await waitFor(() => expect(api.saveTagDefinition).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.saveTagDefinition).mock.calls[0][1].example_image_ids).toEqual([101]);
  });

  it('can remove a staged example that is no longer among the tag\'s positives', async () => {
    // Marked as an example, then relabeled away from this tag on the Labeling
    // page: it is not in the grid, it still counts, and it still saves — so it
    // has to be removable somewhere.
    vi.mocked(api.getTagDefinition).mockResolvedValue({
      data: { ...DEFINITION, example_image_ids: [999] },
    });
    renderPage();
    await selectKuchyne();

    expect(await screen.findByText(/1 staged example not in this list/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Remove image 999 from the examples' }));

    expect(screen.queryByText(/staged example not in this list/)).not.toBeInTheDocument();
    fireEvent.click(saveBtn());
    await waitFor(() => expect(api.saveTagDefinition).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.saveTagDefinition).mock.calls[0][1].example_image_ids).toEqual([]);
  });

  it('refuses to stage more examples than the server would accept', async () => {
    const many: TagPositiveImage[] = Array.from({ length: 26 }, (_, i) => ({
      image_id: 200 + i, storage_path: `img/555/${i}.jpg`,
      sreality_url: 'https://sdn.cz/x.jpg', updated_at: '2026-08-21T00:00:00Z',
    }));
    vi.mocked(api.listTagPositiveImages).mockResolvedValue({ data: many });
    renderPage();
    await selectKuchyne();
    await screen.findByRole('button', { name: 'Toggle image 200 as a canonical example' });

    // Query once, then click: re-querying inside the loop rescans a DOM that
    // grows with every toggle, which made this the one test in the suite that
    // could breach the 5s default timeout on a loaded machine.
    const toggles = many.map((r) =>
      screen.getByRole('button', { name: `Toggle image ${r.image_id} as a canonical example` }),
    );
    for (const toggle of toggles) fireEvent.click(toggle);

    // The cap is the toolkit's EXAMPLE_IMAGES_MAX: clicking past it here is
    // refused, rather than 422-ing the whole sitting at Save.
    expect(screen.getByText(/24 marked as examples \(max 24\)/)).toBeInTheDocument();
    fireEvent.click(saveBtn());
    await waitFor(() => expect(api.saveTagDefinition).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.saveTagDefinition).mock.calls[0][1].example_image_ids).toHaveLength(24);
  });

  it('shows the empty state when a tag has no positive images', async () => {
    vi.mocked(api.listTagPositiveImages).mockResolvedValue({ data: [] });
    renderPage();
    await selectKuchyne();
    expect(
      await screen.findByText('No positive images yet for this tag.'),
    ).toBeInTheDocument();
  });

  // --- overlap evidence ----------------------------------------------------

  it('stages a neighbour into confusable_with without writing', async () => {
    renderPage();
    await selectKuchyne();

    const add = await screen.findByRole('button', { name: 'Add to confusable' });
    expect(screen.getByText('distance 0.123')).toBeInTheDocument();
    fireEvent.click(add);

    expect(screen.getByLabelText('confusable with 2 tag')).toHaveValue('2');
    expect(screen.getByLabelText('confusable with 2 tell')).toHaveValue('');
    expect(api.saveTagDefinition).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'already listed' })).toBeDisabled();
    // A neighbour with no tell yet is not a definition — Save stays blocked
    // until the operator names the difference.
    expect(saveBtn()).toBeDisabled();
  });

  it('says why there is no overlap evidence when the tag has too few embeddings', async () => {
    vi.mocked(api.listTagNeighbours).mockResolvedValue({ data: [] });
    renderPage();
    await selectKuchyne();
    expect(
      await screen.findByText('Needs at least 5 positives with CLIP embeddings to compare.'),
    ).toBeInTheDocument();
  });

  // --- a tag with nothing written yet --------------------------------------

  it('opens an empty draft for a tag with no definition', async () => {
    vi.mocked(api.getTagDefinition).mockResolvedValue({ data: null });
    vi.mocked(api.listTagDefinitionVersions).mockResolvedValue({ data: [] });
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: /koupelna/ }));
    await screen.findByLabelText('means');

    expect(screen.getByLabelText('means')).toHaveValue('');
    expect(screen.getByText('No definition yet — saving writes v1.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save v1' })).toBeDisabled();
    expect(screen.queryByLabelText('Definition history')).not.toBeInTheDocument();
  });
});
