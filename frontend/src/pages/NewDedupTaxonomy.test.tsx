/* NewDedupTaxonomy — the tag-definitions workbench (migration 446,
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
 *
 * Two surfaces deliberately break the one-write rule, and their tests say so:
 * retagging an image out of the tag being read (a tri-state cell is ground
 * truth, not a draft) and renaming a tag in place. What is pinned for those is
 * that the three ways an image can leave a tag never collapse into one, and
 * that neither write refetches a visible list.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import NewDedupTaxonomy from './NewDedupTaxonomy';
import type {
  NewDedupImageTag,
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
import * as toast from '@/lib/toast';
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
    listNewDedupImageTags: vi.fn(),
    setNewDedupTagAnnotation: vi.fn(),
    bulkSetNewDedupImageTags: vi.fn(),
    clearNewDedupTagAnnotation: vi.fn(),
    renameNewDedupTag: vi.fn(),
  };
});

vi.mock('@/lib/queries', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/queries')>();
  return { ...actual, fetchImagesByImageIds: vi.fn() };
});

/* Mocked so "a field-scoped error is NOT also toasted" is assertable — the
 * rename mutation owns its onError precisely to suppress the global toast. */
vi.mock('@/lib/toast', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/toast')>();
  return { ...actual, pushToast: vi.fn() };
});

function tag(over: Partial<NewDedupTag> = {}): NewDedupTag {
  return {
    id: 1, label: 'interier - kuchyne', family: null, active: true,
    priority: false, ready_for_training: false,
    created_at: '2026-08-01T00:00:00Z',
    positive_count: 12, gate_count: 12, border_case_count: 0,
    negative_count: 0, excluded_count: 0, pending_count: 0, dismissed_count: 0,
    candidate_count: 0, candidate_open_count: 0, last_drawn_at: null,
    human_count: 12, machine_count: 0, backfill_count: 0,
    ambiguous_count: 0, ambiguous_decided_count: 0, pruned_count: 0, decided_count: 12,
    ambiguity_rate: 0, ambiguity_alert: false,
    ...over,
  };
}

const KUCHYNE = tag({ id: 1, label: 'interier - kuchyne', priority: true });
const KOUPELNA = tag({ id: 2, label: 'interier - koupelna' });
const FASADA = tag({ id: 3, label: 'exterier - fasada' });

const OVERVIEW: NewDedupLabelingOverview = {
  candidate_image_count: 42,
  ambiguity_threshold: 0.15,
  ambiguity_min_decisions: 20,
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

/* The retag surface's fixtures. Three positives on FASADA (tag 3), because the
 * point of every cache assertion below is that the OTHER two tiles hold still
 * and keep their order when one is moved out. */
const FASADA_POSITIVES: TagPositiveImage[] = [101, 209, 314].map((image_id, i) => ({
  image_id,
  storage_path: `img/555/${i}.jpg`,
  sreality_url: `https://sdn.cz/${image_id}.jpg`,
  updated_at: '2026-08-21T00:00:00Z',
}));

function imageTag(over: Partial<NewDedupImageTag> = {}): NewDedupImageTag {
  return {
    id: 1, label: 'interier - kuchyne', family: 'interier', state: 'untouched',
    updated_at: null, source: null, excluded_reason: null,
    ...over,
  };
}

/* Every active tag on image 101, with FASADA — the tag being read — positive.
 * That row is the subject: pinned at the top, never repeated in its family. */
const IMAGE_TAGS: NewDedupImageTag[] = [
  imageTag({ id: 1, label: 'interier - kuchyne', family: 'interier', state: 'untouched' }),
  imageTag({ id: 2, label: 'interier - koupelna', family: 'interier', state: 'untouched' }),
  imageTag({
    id: 3, label: 'exterier - fasada', family: 'exterier',
    state: 'positive', updated_at: 't', source: 'human',
  }),
];

function annotationResult(
  over: Partial<{
    image_id: number; tag_id: number; state: api.TagState;
    source: api.TagSource; excluded_reason: api.TagExcludedReason | null;
  }> = {},
) {
  return {
    data: {
      image_id: 101, tag_id: 3, state: 'excluded' as api.TagState,
      source: 'human' as api.TagSource,
      excluded_reason: 'pruned' as api.TagExcludedReason | null,
      definition_id: 7, verified_at: '2026-08-27T00:00:00Z', updated_at: 't', applied: true,
      ...over,
    },
  };
}

/* The seven fields tag_annotations._tag_dict actually returns — no counts. A
 * cache patch that spread this over a cached row would render NaN bars, which
 * is exactly what the narrowed type and the merge-two-fields patch prevent. */
const renamed = (id: number, label: string): api.NewDedupTagIdentity => ({
  id, label, family: null, active: true,
  priority: false, ready_for_training: false, created_at: '2026-08-01T00:00:00Z',
});

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

/* main.tsx's REAL query defaults. The suite's client above leaves staleTime at
 * 0, which refetches everything on remount and would let a cache this page
 * mutilated pass as correct. The three tests that are about what the next visit
 * to a tag SERVES have to run against the live numbers or they pin nothing. */
function renderPageWithProductionCache() {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { staleTime: 60_000, gcTime: 5 * 60_000, refetchOnWindowFocus: false, retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/new-dedup/labeling/taxonomy']}>
        <NewDedupTaxonomy />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/* Per-tag positives, so a test can watch what a SECOND tag's gallery holds. */
const positivesByTag = (map: Record<number, TagPositiveImage[]>) =>
  vi.mocked(api.listTagPositiveImages).mockImplementation(async (tagId: number) => ({
    data: map[tagId] ?? [],
  }));
const positiveCallsFor = (tagId: number) =>
  vi.mocked(api.listTagPositiveImages).mock.calls.filter((c) => c[0] === tagId).length;

/* Selection by row, waiting on the EDITOR HEADING rather than on `means`:
 * flipping back to a tag already on screen leaves `means` mounted throughout,
 * so only the heading proves the switch actually landed. */
async function selectRow(match: RegExp, heading: string) {
  fireEvent.click(await screen.findByRole('button', { name: match }));
  await screen.findByRole('heading', { name: heading });
}

/* Selecting a tag is how every test past the first one starts: the right column
 * renders nothing until one is picked. */
async function selectKuchyne() {
  const row = await screen.findByRole('button', { name: /kuchyne/ });
  fireEvent.click(row);
  await screen.findByLabelText('means');
}

/* The tag the operator is looking at in the live case this work came from:
 * "exterier - fasáda", whose gallery is the wall of photos being read. */
async function selectFasada() {
  vi.mocked(api.listTagPositiveImages).mockResolvedValue({ data: FASADA_POSITIVES });
  const row = await screen.findByRole('button', { name: /fasada/ });
  fireEvent.click(row);
  await screen.findByLabelText('means');
  await screen.findByRole('button', { name: 'All tags on image 101' });
}

/* Opens the shared all-tags panel over one gallery tile. */
async function openAllTags(imageId = 101) {
  fireEvent.click(screen.getByRole('button', { name: `All tags on image ${imageId}` }));
  const panel = await screen.findByRole('dialog', { name: 'All tags on this image' });
  await within(panel).findByRole('group', { name: "This tag's state" });
  return panel;
}

const outcome = (panel: HTMLElement, name: string) =>
  within(within(panel).getByRole('group', { name: "This tag's state" })).getByRole('button', {
    name,
  });

const saveBtn = () => screen.getByRole('button', { name: /^Save v/ });
const tileSrcs = () =>
  screen
    .getAllByRole('button', { name: /^Toggle image \d+ as a canonical example$/ })
    .map((b) => b.querySelector('img')?.getAttribute('src') ?? '');

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
    vi.mocked(api.listNewDedupImageTags).mockResolvedValue({ data: IMAGE_TAGS });
    vi.mocked(api.setNewDedupTagAnnotation).mockResolvedValue(annotationResult());
    vi.mocked(api.bulkSetNewDedupImageTags).mockResolvedValue({
      data: { updated: 2, image_id: 101, state: 'negative', excluded_reason: null, tag_ids: [1, 2] },
    });
    vi.mocked(api.renameNewDedupTag).mockResolvedValue({
      data: renamed(3, 'exterier - fasada a sokl'),
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

  it('keeps the truncation note true after an image is moved out', async () => {
    // A fetch that came back holding the cap: the list IS truncated. Moving one
    // image out drops the live count to 299, and if the note is read off THAT
    // the page silently starts claiming the operator has seen everything — on
    // the one surface whose entire claim is what a tag really holds.
    const capped: TagPositiveImage[] = Array.from({ length: 300 }, (_, i) => ({
      image_id: i === 0 ? 101 : 1000 + i,
      storage_path: `img/9/${i}.jpg`,
      sreality_url: 'https://sdn.cz/x.jpg',
      updated_at: '2026-08-21T00:00:00Z',
    }));
    vi.mocked(api.listTagPositiveImages).mockResolvedValue({ data: capped });
    renderPage();
    await selectRow(/fasada/, 'exterier - fasada');
    await screen.findByRole('button', { name: 'All tags on image 101' });
    expect(screen.getByText('showing the 300 most recent')).toBeInTheDocument();

    const panel = await openAllTags();
    fireEvent.click(outcome(panel, 'belongs elsewhere'));

    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: 'Toggle image 101 as a canonical example' }),
      ).toBeNull(),
    );
    expect(screen.getByText(/299 positive images/)).toBeInTheDocument();
    expect(screen.getByText('showing the 300 most recent')).toBeInTheDocument();
    // Hitting the cap is the point, so this renders 300 tiles plus the 51-row
    // panel and re-renders them all on the move-out. That is ~5s in jsdom —
    // genuinely heavy work, not a slow assertion — and it straddled the 5s
    // default (5136ms passing, 5866ms failing on the same machine).
  }, 20_000);

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

  // --- retagging from the gallery: the affordance ---------------------------

  it('offers an "all tags" control on every tile without hijacking the example click', async () => {
    renderPage();
    await selectFasada();

    const tile = screen.getByRole('button', {
      name: 'Toggle image 101 as a canonical example',
    });
    fireEvent.click(screen.getByRole('button', { name: 'All tags on image 101' }));

    await screen.findByRole('dialog', { name: 'All tags on this image' });
    // The tile-wide "mark as example" click is untouched, and nothing staged.
    expect(tile).toHaveAttribute('aria-pressed', 'false');
    expect(api.saveTagDefinition).not.toHaveBeenCalled();
  });

  it('still stages a canonical example when the tile itself is clicked', async () => {
    renderPage();
    await selectFasada();

    const tile = screen.getByRole('button', {
      name: 'Toggle image 101 as a canonical example',
    });
    fireEvent.click(tile);

    expect(tile).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByRole('dialog', { name: 'All tags on this image' })).toBeNull();
  });

  it('opens the panel on the tile that was clicked', async () => {
    renderPage();
    await selectFasada();
    const panel = await openAllTags(209);

    expect(api.listNewDedupImageTags).toHaveBeenCalledWith(209);
    expect(within(panel).getByText('Image 209 — all tags')).toBeInTheDocument();
  });

  // --- retagging: the three outcomes must not collapse ----------------------

  it('writes "belongs elsewhere" as excluded · pruned, never as a negative', async () => {
    renderPage();
    await selectFasada();
    const panel = await openAllTags();

    fireEvent.click(outcome(panel, 'belongs elsewhere'));

    await waitFor(() =>
      expect(api.setNewDedupTagAnnotation).toHaveBeenCalledWith(3, 101, 'excluded', 'pruned'),
    );
  });

  it('writes "not this tag" as a real negative with no reason', async () => {
    vi.mocked(api.setNewDedupTagAnnotation).mockResolvedValue(
      annotationResult({ state: 'negative', excluded_reason: null }),
    );
    renderPage();
    await selectFasada();
    const panel = await openAllTags();

    fireEvent.click(outcome(panel, 'not this tag'));

    await waitFor(() =>
      expect(api.setNewDedupTagAnnotation).toHaveBeenCalledWith(3, 101, 'negative', null),
    );
  });

  it('writes "can\'t tell" as excluded · ambiguous', async () => {
    vi.mocked(api.setNewDedupTagAnnotation).mockResolvedValue(
      annotationResult({ state: 'excluded', excluded_reason: 'ambiguous' }),
    );
    renderPage();
    await selectFasada();
    const panel = await openAllTags();

    fireEvent.click(outcome(panel, "can't tell"));

    await waitFor(() =>
      expect(api.setNewDedupTagAnnotation).toHaveBeenCalledWith(3, 101, 'excluded', 'ambiguous'),
    );
  });

  it('cannot clear the cell back to untouched', async () => {
    renderPage();
    await selectFasada();
    const panel = await openAllTags();

    // Four outcomes, and none of them is "forget that a human looked".
    const group = within(panel).getByRole('group', { name: "This tag's state" });
    expect(within(group).getAllByRole('button')).toHaveLength(4);
    fireEvent.click(outcome(panel, 'belongs elsewhere'));
    await waitFor(() => expect(api.setNewDedupTagAnnotation).toHaveBeenCalled());
    expect(api.clearNewDedupTagAnnotation).not.toHaveBeenCalled();
  });

  it('says what each outcome will do before it is clicked', async () => {
    renderPage();
    await selectFasada();
    const panel = await openAllTags();

    // The rule, always on screen — no hover required.
    expect(within(panel).getByText(/never negative/)).toBeInTheDocument();
    expect(outcome(panel, 'belongs elsewhere')).toHaveAttribute(
      'title',
      expect.stringMatching(/another tag fits better/),
    );
    expect(outcome(panel, 'not this tag')).toHaveAttribute(
      'title',
      expect.stringMatching(/real, valuable negative/),
    );
  });

  it('renders the tag being read exactly once — pinned, not repeated in its family', async () => {
    renderPage();
    await selectFasada();
    const panel = await openAllTags();

    expect(within(panel).getAllByRole('group', { name: "This tag's state" })).toHaveLength(1);
    // One control over one cell: the other two tags keep the three-glyph one,
    // and "exterier" has no group of its own because fasada was its only row.
    expect(within(panel).getAllByRole('group', { name: 'Tag state' })).toHaveLength(
      IMAGE_TAGS.length - 1,
    );
    expect(within(panel).queryByText('exterier')).toBeNull();
    expect(within(panel).getByText('interier')).toBeInTheDocument();
  });

  it('moves the image UNDER another tag from the same panel', async () => {
    vi.mocked(api.setNewDedupTagAnnotation).mockResolvedValue(
      annotationResult({ tag_id: 1, state: 'positive', excluded_reason: null }),
    );
    renderPage();
    await selectFasada();
    const panel = await openAllTags();

    const kuchyne = within(panel).getAllByRole('group', { name: 'Tag state' })[0];
    fireEvent.click(within(kuchyne).getAllByRole('button')[0]);

    await waitFor(() =>
      expect(api.setNewDedupTagAnnotation).toHaveBeenCalledWith(1, 101, 'positive', null),
    );
  });

  it('keeps the batch action off the tag being read', async () => {
    renderPage();
    await selectFasada();
    const panel = await openAllTags();

    fireEvent.click(within(panel).getByRole('button', { name: 'Select all untouched' }));
    expect(within(panel).getByText('2 selected')).toBeInTheDocument();
    fireEvent.click(within(panel).getByRole('button', { name: 'Set selected: negative' }));

    await waitFor(() =>
      expect(api.bulkSetNewDedupImageTags).toHaveBeenCalledWith(101, [1, 2], 'negative', null),
    );
  });

  it('draws a 442-manufactured positive as the default it is', async () => {
    vi.mocked(api.listNewDedupImageTags).mockResolvedValue({
      data: IMAGE_TAGS.map((t) =>
        t.id === 3 ? { ...t, source: 'backfill_442' as const } : t,
      ),
    });
    renderPage();
    await selectFasada();
    const panel = await openAllTags();

    const keeps = outcome(panel, 'keeps it');
    expect(keeps).toHaveAttribute('aria-pressed', 'true');
    expect(keeps.className).toContain('border-dashed');
    expect(keeps).toHaveAttribute('title', expect.stringMatching(/migration 442/));
  });

  it('says so when the tag being read is inactive, instead of omitting the block', async () => {
    // GET /images/{id}/tags returns ACTIVE tags only, so an inactive subject is
    // simply absent — a missing block would read as "not on this tag at all".
    vi.mocked(api.listNewDedupImageTags).mockResolvedValue({
      data: IMAGE_TAGS.filter((t) => t.id !== 3),
    });
    renderPage();
    await selectFasada();
    fireEvent.click(screen.getByRole('button', { name: 'All tags on image 101' }));
    const panel = await screen.findByRole('dialog', { name: 'All tags on this image' });

    expect(
      await within(panel).findByText(
        'This tag is not in the active list, so its state cannot be set here.',
      ),
    ).toBeInTheDocument();
    expect(within(panel).queryByRole('button', { name: 'belongs elsewhere' })).toBeNull();
  });

  // --- retagging: cache policy ---------------------------------------------

  it('drops the tile from the grid without refetching the grid', async () => {
    renderPage();
    await selectFasada();
    const panel = await openAllTags();
    fireEvent.click(outcome(panel, 'belongs elsewhere'));

    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: 'Toggle image 101 as a canonical example' }),
      ).toBeNull(),
    );
    expect(screen.getByText(/2 positive images/)).toBeInTheDocument();
    expect(vi.mocked(api.listTagPositiveImages).mock.calls).toHaveLength(1);
  });

  it('disturbs no other tile and reorders nothing', async () => {
    renderPage();
    await selectFasada();
    const before = tileSrcs();
    const panel = await openAllTags();
    fireEvent.click(outcome(panel, 'belongs elsewhere'));

    await waitFor(() => expect(tileSrcs()).toHaveLength(2));
    expect(tileSrcs()).toEqual(before.slice(1));
    for (const id of [209, 314]) {
      expect(
        screen.getByRole('button', { name: `Toggle image ${id} as a canonical example` }),
      ).toHaveAttribute('aria-pressed', 'false');
    }
  });

  it('takes the moved counts from the server rather than recomputing them', async () => {
    // ambiguity_rate has exactly ONE definition, server-side. The overview is
    // invalidated, never patched — so whatever the server says is what shows.
    vi.mocked(api.getNewDedupLabelingOverview)
      .mockResolvedValueOnce({ data: OVERVIEW })
      .mockResolvedValue({
        data: { ...OVERVIEW, tags: [KUCHYNE, KOUPELNA, { ...FASADA, positive_count: 99 }] },
      });
    renderPage();
    await selectFasada();
    const panel = await openAllTags();
    fireEvent.click(outcome(panel, 'belongs elsewhere'));

    await waitFor(() =>
      expect(vi.mocked(api.getNewDedupLabelingOverview).mock.calls.length).toBeGreaterThan(1),
    );
    const row = await screen.findByRole('button', { name: /fasada/ });
    await waitFor(() => expect(within(row).getByText('99')).toBeInTheDocument());
  });

  it('does not refetch the overlap evidence for a state change', async () => {
    renderPage();
    await selectFasada();
    const panel = await openAllTags();
    fireEvent.click(outcome(panel, 'belongs elsewhere'));

    await waitFor(() => expect(api.setNewDedupTagAnnotation).toHaveBeenCalled());
    expect(vi.mocked(api.listTagNeighbours).mock.calls).toHaveLength(1);
  });

  it('neither refetches nor dirties the definition', async () => {
    renderPage();
    await selectFasada();
    const panel = await openAllTags();
    fireEvent.click(outcome(panel, 'belongs elsewhere'));

    await waitFor(() => expect(api.setNewDedupTagAnnotation).toHaveBeenCalled());
    expect(vi.mocked(api.getTagDefinition).mock.calls).toHaveLength(1);
    expect(saveBtn()).toBeDisabled();
    expect(screen.queryByText(/unsaved/)).not.toBeInTheDocument();
  });

  it('names what happened in a strip that can undo it', async () => {
    renderPage();
    await selectFasada();
    const panel = await openAllTags();
    fireEvent.click(outcome(panel, 'belongs elsewhere'));

    expect(await screen.findByText(/1 image moved out of this tag/)).toBeInTheDocument();
    // The chip names the outcome in the same words the panel's button used.
    expect(screen.getByText('· belongs elsewhere')).toBeInTheDocument();

    vi.mocked(api.setNewDedupTagAnnotation).mockResolvedValue(
      annotationResult({ state: 'positive', excluded_reason: null }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'put back' }));

    await waitFor(() =>
      expect(api.setNewDedupTagAnnotation).toHaveBeenLastCalledWith(3, 101, 'positive', null),
    );
    // Back at its ORIGINAL index — the grid does not reshuffle under the cursor.
    await waitFor(() =>
      expect(
        screen.getAllByRole('button', { name: /^Toggle image \d+ as a canonical example$/ })[0],
      ).toHaveAccessibleName('Toggle image 101 as a canonical example'),
    );
    expect(screen.queryByText(/moved out of this tag/)).toBeNull();
  });

  it('puts the row back verbatim, with no refetch', async () => {
    renderPage();
    await selectFasada();
    const before = tileSrcs();
    const panel = await openAllTags();
    fireEvent.click(outcome(panel, 'belongs elsewhere'));
    await waitFor(() => expect(tileSrcs()).toHaveLength(2));

    vi.mocked(api.setNewDedupTagAnnotation).mockResolvedValue(
      annotationResult({ state: 'positive', excluded_reason: null }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'put back' }));

    await waitFor(() => expect(tileSrcs()).toEqual(before));
    expect(vi.mocked(api.listTagPositiveImages).mock.calls).toHaveLength(1);
  });

  it('leaves no other tag\'s gallery contradicting the count beside it', async () => {
    // The panel's second half writes to tags that are NOT on screen. Their
    // galleries are cached, and the overview refetch has already moved their
    // counts in the list on the left — so the next visit must not serve a
    // gallery this write contradicted.
    positivesByTag({ 1: POSITIVES, 3: FASADA_POSITIVES });
    vi.mocked(api.setNewDedupTagAnnotation).mockResolvedValue(
      annotationResult({ tag_id: 1, state: 'positive', excluded_reason: null }),
    );
    renderPageWithProductionCache();
    await selectRow(/kuchyne/, 'interier - kuchyne');
    await screen.findByRole('button', { name: 'All tags on image 101' });
    expect(positiveCallsFor(1)).toBe(1);

    await selectRow(/fasada/, 'exterier - fasada');
    // 314 is fasada's alone — image 101 sits in both galleries, so waiting on
    // it would resolve against the grid this switch is still replacing.
    await screen.findByRole('button', { name: 'All tags on image 314' });
    const panel = await openAllTags();
    const kuchyne = within(panel).getAllByRole('group', { name: 'Tag state' })[0];
    fireEvent.click(within(kuchyne).getAllByRole('button')[0]);
    await waitFor(() =>
      expect(api.setNewDedupTagAnnotation).toHaveBeenCalledWith(1, 101, 'positive', null),
    );
    fireEvent.click(within(panel).getByRole('button', { name: 'Close' }));

    await selectRow(/kuchyne/, 'interier - kuchyne');
    await waitFor(() => expect(positiveCallsFor(1)).toBe(2));
    // Only the tag that was written to: the one being read is still patched in
    // place, never refetched.
    expect(positiveCallsFor(3)).toBe(1);
  });

  it('repairs the grid when a put-back lands after a tag switch', async () => {
    positivesByTag({ 2: [], 3: FASADA_POSITIVES });
    renderPageWithProductionCache();
    await selectRow(/fasada/, 'exterier - fasada');
    await screen.findByRole('button', { name: 'All tags on image 101' });
    const panel = await openAllTags();
    fireEvent.click(outcome(panel, 'belongs elsewhere'));
    await waitFor(() => expect(tileSrcs()).toHaveLength(2));
    fireEvent.click(within(panel).getByRole('button', { name: 'Close' }));

    // Hold the put-back open across the tag switch that empties the receipt
    // strip — there is then no held row to splice back.
    let release: () => void = () => {};
    vi.mocked(api.setNewDedupTagAnnotation).mockImplementation(
      () =>
        new Promise((resolve) => {
          release = () =>
            resolve(annotationResult({ state: 'positive', excluded_reason: null }).data);
        }).then((data) => ({ data })) as ReturnType<typeof api.setNewDedupTagAnnotation>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'put back' }));
    await selectRow(/koupelna/, 'interier - koupelna');
    release();
    await waitFor(() =>
      expect(api.setNewDedupTagAnnotation).toHaveBeenLastCalledWith(3, 101, 'positive', null),
    );

    // The server holds image 101 positive again. The cached grid must not still
    // be short of it, with no strip left to say why.
    await selectRow(/fasada/, 'exterier - fasada');
    await waitFor(() => expect(tileSrcs()).toHaveLength(3));
  });

  it('keeps the moved-out strip session-local', async () => {
    renderPage();
    await selectFasada();
    const panel = await openAllTags();
    fireEvent.click(outcome(panel, 'belongs elsewhere'));
    await screen.findByText(/1 image moved out of this tag/);
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));

    fireEvent.click(screen.getByRole('button', { name: /koupelna/ }));
    await waitFor(() => expect(api.getTagDefinition).toHaveBeenCalledWith(2));
    fireEvent.click(screen.getByRole('button', { name: /fasada/ }));
    await waitFor(() => expect(api.getTagDefinition).toHaveBeenLastCalledWith(3));

    expect(screen.queryByText(/moved out of this tag/)).toBeNull();
  });

  it('never silently edits the draft when a staged example is moved out', async () => {
    renderPage();
    await selectFasada();
    fireEvent.click(
      screen.getByRole('button', { name: 'Toggle image 101 as a canonical example' }),
    );
    const panel = await openAllTags();
    fireEvent.click(outcome(panel, 'belongs elsewhere'));

    expect(await screen.findByText('still staged as a canonical example')).toBeInTheDocument();
    expect(screen.getByText(/1 staged example not in this list/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    fireEvent.click(saveBtn());
    await waitFor(() => expect(api.saveTagDefinition).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.saveTagDefinition).mock.calls[0][1].example_image_ids).toEqual([101]);
  });

  // --- renaming a tag in place ---------------------------------------------

  it('offers rename on the selected tag only', async () => {
    renderPage();
    await screen.findByRole('button', { name: /kuchyne/ });
    expect(screen.queryByRole('button', { name: 'Rename this tag' })).toBeNull();

    await selectKuchyne();
    expect(screen.getAllByRole('button', { name: 'Rename this tag' })).toHaveLength(1);
  });

  it('seeds the field with the FULL label, family prefix included', async () => {
    renderPage();
    await selectKuchyne();
    fireEvent.click(screen.getByRole('button', { name: 'Rename this tag' }));

    // The list renders "kuchyne"; seeding the field with that would silently
    // destroy the family prefix and move the tag out of its group.
    expect(screen.getByLabelText('New tag label')).toHaveValue('interier - kuchyne');
    expect(screen.getByText(/is the family — changing it moves the tag/)).toBeInTheDocument();
  });

  it('commits the rename on Enter', async () => {
    vi.mocked(api.renameNewDedupTag).mockResolvedValue({
      data: renamed(1, 'interier - kuchynka'),
    });
    renderPage();
    await selectKuchyne();
    fireEvent.click(screen.getByRole('button', { name: 'Rename this tag' }));

    const field = screen.getByLabelText('New tag label');
    fireEvent.change(field, { target: { value: 'interier - kuchynka' } });
    fireEvent.keyDown(field, { key: 'Enter' });

    await waitFor(() => expect(api.renameNewDedupTag).toHaveBeenCalledTimes(1));
    expect(api.renameNewDedupTag).toHaveBeenCalledWith(1, 'interier - kuchynka');
  });

  it('commits from Save, and reverts from Cancel or Escape without writing', async () => {
    renderPage();
    await selectKuchyne();

    for (const dismiss of ['Cancel', 'Escape'] as const) {
      fireEvent.click(screen.getByRole('button', { name: 'Rename this tag' }));
      const field = screen.getByLabelText('New tag label');
      fireEvent.change(field, { target: { value: 'interier - something else' } });
      if (dismiss === 'Cancel') fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      else fireEvent.keyDown(field, { key: 'Escape' });
      expect(screen.queryByLabelText('New tag label')).toBeNull();
      expect(api.renameNewDedupTag).not.toHaveBeenCalled();
    }

    fireEvent.click(screen.getByRole('button', { name: 'Rename this tag' }));
    fireEvent.change(screen.getByLabelText('New tag label'), {
      target: { value: 'interier - kuchynka' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(api.renameNewDedupTag).toHaveBeenCalledWith(1, 'interier - kuchynka'));
  });

  it('does not re-open an abandoned rename, autofocused, on the next visit', async () => {
    renderPage();
    await selectKuchyne();
    fireEvent.click(screen.getByRole('button', { name: 'Rename this tag' }));
    fireEvent.change(screen.getByLabelText('New tag label'), {
      target: { value: 'interier - ABANDONED' },
    });

    // Clicked away, no Cancel and no Escape — which only HIDES the editor.
    await selectRow(/koupelna/, 'interier - koupelna');
    await selectRow(/kuchyne/, 'interier - kuchyne');

    // Nothing holding the abandoned text, so the operator's next Enter cannot
    // commit a label they walked away from — the same accident the deliberate
    // no-commit-on-blur guards against, arriving by the other door.
    expect(screen.queryByLabelText('New tag label')).toBeNull();
    fireEvent.keyDown(document.body, { key: 'Enter' });
    expect(api.renameNewDedupTag).not.toHaveBeenCalled();
  });

  it('does not carry a dead rename error back to the tag it came from', async () => {
    vi.mocked(api.renameNewDedupTag).mockRejectedValue(new Error('BOOM already exists'));
    renderPage();
    await selectKuchyne();
    fireEvent.click(screen.getByRole('button', { name: 'Rename this tag' }));
    fireEvent.change(screen.getByLabelText('New tag label'), {
      target: { value: 'interier - koupelna' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByText('BOOM already exists')).toBeInTheDocument();

    await selectRow(/koupelna/, 'interier - koupelna');
    await selectRow(/kuchyne/, 'interier - kuchyne');

    expect(screen.queryByText('BOOM already exists')).toBeNull();
  });

  it('refuses a blank or unchanged label', async () => {
    renderPage();
    await selectKuchyne();
    fireEvent.click(screen.getByRole('button', { name: 'Rename this tag' }));
    const field = screen.getByLabelText('New tag label');

    // Unchanged: a no-op write is not a write.
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    fireEvent.keyDown(field, { key: 'Enter' });

    fireEvent.change(field, { target: { value: '   ' } });
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    fireEvent.keyDown(field, { key: 'Enter' });

    expect(api.renameNewDedupTag).not.toHaveBeenCalled();
  });

  it('enforces the server\'s label cap at the input', async () => {
    renderPage();
    await selectKuchyne();
    fireEvent.click(screen.getByRole('button', { name: 'Rename this tag' }));
    // Mirrors toolkit.tag_annotations.LABEL_MAX_CHARS / migration 442's CHECK.
    expect(screen.getByLabelText('New tag label')).toHaveAttribute('maxlength', '100');
  });

  it('surfaces a duplicate beside the field, keeps the typing, and does not toast', async () => {
    vi.mocked(api.renameNewDedupTag).mockRejectedValue(
      new Error("tag 'interier - koupelna' already exists"),
    );
    renderPage();
    await selectKuchyne();
    fireEvent.click(screen.getByRole('button', { name: 'Rename this tag' }));
    fireEvent.change(screen.getByLabelText('New tag label'), {
      target: { value: 'interier - koupelna' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    expect(
      await screen.findByText("tag 'interier - koupelna' already exists"),
    ).toBeInTheDocument();
    // Still editing (the field, not the "rename" button, is what is on screen),
    // still holding the typing, and the page still reads the OLD label.
    expect(screen.getByLabelText('New tag label')).toHaveValue('interier - koupelna');
    expect(screen.getByRole('heading', { name: 'interier - kuchyne' })).toBeInTheDocument();
    // The field is on screen and focused — a toast six seconds away would be
    // the same message said twice, so the mutation owns its onError.
    expect(vi.mocked(toast.pushToast)).not.toHaveBeenCalled();
  });

  it('patches the visible list rather than refetching it', async () => {
    renderPage();
    await selectFasada();
    fireEvent.click(screen.getByRole('button', { name: 'Rename this tag' }));
    fireEvent.change(screen.getByLabelText('New tag label'), {
      target: { value: 'exterier - fasada a sokl' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(api.renameNewDedupTag).toHaveBeenCalled());
    // Sidebar row (short label), editor heading (full label) — one patch, no
    // refetch of a 51-row list.
    expect(await screen.findByRole('button', { name: /fasada a sokl/ })).toBeInTheDocument();
    expect(
      screen.getByRole('heading', { name: 'exterier - fasada a sokl' }),
    ).toBeInTheDocument();
    const picker = screen.getByLabelText('confusable with 1 tag');
    expect(
      within(picker).getAllByRole('option').map((o) => o.textContent),
    ).toContain('fasada a sokl');
    expect(vi.mocked(api.getNewDedupLabelingOverview).mock.calls).toHaveLength(1);
  });

  it('loses neither the operator\'s place nor their unsaved writing', async () => {
    renderPage();
    await selectFasada();
    fireEvent.change(screen.getByLabelText('means'), {
      target: { value: 'The street-facing wall of a building.' },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Rename this tag' }));
    fireEvent.change(screen.getByLabelText('New tag label'), {
      target: { value: 'exterier - fasada a sokl' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(api.renameNewDedupTag).toHaveBeenCalled());

    // The draft holds no label text — every tag reference is an id — so a
    // rename cannot reload the form or move base_version.
    expect(screen.getByLabelText('means')).toHaveValue('The street-facing wall of a building.');
    expect(vi.mocked(api.getTagDefinition).mock.calls).toHaveLength(1);
    expect(saveBtn()).toBeEnabled();
    fireEvent.click(saveBtn());
    await waitFor(() => expect(api.saveTagDefinition).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.saveTagDefinition).mock.calls[0][1].base_version).toBe(2);
  });

  it('leaves no stale label in the overlap evidence', async () => {
    // The neighbours query is a centroid + pgvector scan, so it is NOT re-run
    // for a text change; OverlapEvidence reads the label through the live
    // taxonomy instead. (This fixture's neighbour is the selected tag itself —
    // what matters is that the row's own `label` is not what renders.)
    vi.mocked(api.renameNewDedupTag).mockResolvedValue({
      data: renamed(2, 'interier - koupelna a wc'),
    });
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: /koupelna/ }));
    await screen.findByLabelText('means');
    const overlapRow = (await screen.findByText('distance 0.123')).closest(
      'li',
    ) as HTMLElement;
    expect(within(overlapRow).getByText('interier - koupelna')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Rename this tag' }));
    fireEvent.change(screen.getByLabelText('New tag label'), {
      target: { value: 'interier - koupelna a wc' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(within(overlapRow).getByText('interier - koupelna a wc')).toBeInTheDocument(),
    );
    expect(vi.mocked(api.listTagNeighbours).mock.calls).toHaveLength(1);
  });

  it('does not make the row ambiguous, and rename does not change the selection', async () => {
    renderPage();
    await selectFasada();

    expect(screen.getAllByRole('button', { name: /fasada/ })).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: 'Rename this tag' }));
    // Still fasada's definition on screen; nothing re-selected, nothing refetched.
    expect(vi.mocked(api.getTagDefinition).mock.calls).toHaveLength(1);
    expect(api.getTagDefinition).toHaveBeenCalledWith(3);
  });

  it('never wipes the counts when patching from a partial rename response', async () => {
    vi.mocked(api.renameNewDedupTag).mockResolvedValue({
      data: renamed(1, 'interier - kuchynka'),
    });
    renderPage();
    await selectKuchyne();
    fireEvent.click(screen.getByRole('button', { name: 'Rename this tag' }));
    fireEvent.change(screen.getByLabelText('New tag label'), {
      target: { value: 'interier - kuchynka' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    const row = await screen.findByRole('button', { name: /kuchynka/ });
    // _tag_dict carries no counts: spreading the response would render NaN.
    expect(within(row).getByText('12')).toBeInTheDocument();
    expect(within(row).getByText('v2')).toBeInTheDocument();
  });

  // --- the two surfaces together -------------------------------------------

  it('shows the new label in the all-tags panel after a rename', async () => {
    renderPage();
    await selectFasada();
    const panel = await openAllTags();
    fireEvent.click(within(panel).getByRole('button', { name: 'Close' }));

    fireEvent.click(screen.getByRole('button', { name: 'Rename this tag' }));
    fireEvent.change(screen.getByLabelText('New tag label'), {
      target: { value: 'exterier - fasada a sokl' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(api.renameNewDedupTag).toHaveBeenCalled());

    vi.mocked(api.listNewDedupImageTags).mockResolvedValue({
      data: IMAGE_TAGS.map((t) => (t.id === 3 ? { ...t, label: 'exterier - fasada a sokl' } : t)),
    });
    const reopened = await openAllTags();

    await waitFor(() =>
      expect(vi.mocked(api.listNewDedupImageTags).mock.calls).toHaveLength(2),
    );
    expect(
      within(reopened).getByText('exterier - fasada a sokl'),
    ).toBeInTheDocument();
  });
});
