/* NewDedupLabeling — the tag annotation matrix's labeling page
 * (docs/design/tag-annotation-matrix.md).
 *
 * Hermetic: mock every /new-dedup/labeling api.ts call, listNewDedupSettings,
 * and the Supabase image read (fetchImagesByImageIds). Pins the WIRING, not the
 * backend — CRUD is covered by tests/api/test_new_dedup_labeling_routes.py and
 * tests/toolkit/test_tag_annotations.py.
 *
 * The page has two grids over one substrate:
 *   - Proposals — the secondary-CLIP suggestion queue. A tile's tri-state
 *     control writes through setNewDedupProposalState, which ALSO flips the
 *     proposal's bookkeeping status.
 *   - Sample — every image in the labeling pool for ONE tag, including ones no
 *     model ever proposed it for. Writes through setNewDedupTagAnnotation,
 *     which is a plain idempotent upsert on image_tag_labels.
 *
 * Re-deciding an already-decided proposal (clicking a different tri-state
 * button on a Confirmed/Dismissed tile) is allowed server-side —
 * set_proposal_state has no pending-only guard, since there is only ONE
 * write path into image_tag_labels here and a repeat call can't diverge
 * anything. It overwrites both the proposal's bookkeeping status and the
 * annotation together, same as a first decision.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import NewDedupLabeling from './NewDedupLabeling';
import type {
  NewDedupImageTag,
  NewDedupLabelingOverview,
  NewDedupLabelProposal,
  NewDedupProposalStateResult,
  NewDedupSetting,
  NewDedupTag,
  NewDedupTagImage,
  TagState,
} from '@/lib/api';
import * as api from '@/lib/api';
import * as queries from '@/lib/queries';
import type { ImagePublic } from '@/lib/types';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    getNewDedupLabelingOverview: vi.fn(),
    addNewDedupTag: vi.fn(),
    renameNewDedupTag: vi.fn(),
    removeNewDedupTag: vi.fn(),
    setNewDedupTagFlags: vi.fn(),
    growNewDedupSample: vi.fn(),
    listNewDedupProposals: vi.fn(),
    listNewDedupOriginalTags: vi.fn(),
    setNewDedupProposalState: vi.fn(),
    bulkSetNewDedupProposalState: vi.fn(),
    listNewDedupTagImages: vi.fn(),
    setNewDedupTagAnnotation: vi.fn(),
    bulkSetNewDedupTagAnnotation: vi.fn(),
    listNewDedupImageTags: vi.fn(),
    bulkSetNewDedupImageTags: vi.fn(),
    listNewDedupPositiveTagsForImages: vi.fn(),
    listNewDedupSettings: vi.fn(),
    setBorderCase: vi.fn(),
    deleteBorderCase: vi.fn(),
  };
});

vi.mock('@/lib/queries', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/queries')>();
  return {
    ...actual,
    fetchImagesByImageIds: vi.fn(),
    fetchBorderCasesByImageIds: vi.fn(),
  };
});

const MODEL = 'openai/clip-vit-large-patch14';

const SETTINGS: NewDedupSetting[] = [
  {
    key: 'labeling_secondary_model', category: 'labeling', value_type: 'text',
    value: MODEL, default: MODEL,
    is_override: false, decided: false, explanation: 'The stronger CLIP checkpoint.',
    enum_choices: null, minimum: null, maximum: null,
  },
  {
    key: 'labeling_target_proposals_per_category', category: 'labeling', value_type: 'integer',
    value: 300, default: 300, is_override: false, decided: true,
    explanation: 'How many proposals a tag needs.',
    enum_choices: null, minimum: 1, maximum: null,
  },
  {
    key: 'labeling_gate1_target_per_tag', category: 'labeling', value_type: 'integer',
    value: 150, default: 150, is_override: false, decided: true,
    explanation: 'Wave 1 gate.', enum_choices: null, minimum: 1, maximum: null,
  },
];

/* One tag, with a count in each of the three states — the shape the whole page
 * now reads (`data.tags`, never `data.labels`). */
function tag(over: Partial<NewDedupTag> = {}): NewDedupTag {
  return {
    id: 1, label: 'interier - kuchyne', family: 'interier', active: true,
    priority: false, ready_for_training: false,
    created_at: '2026-08-01T00:00:00Z',
    positive_count: 12, gate_count: 12, border_case_count: 0,
    negative_count: 8, excluded_count: 5,
    pending_count: 3, dismissed_count: 1,
    ...over,
  };
}

const OVERVIEW: NewDedupLabelingOverview = { sample_size: 42, tags: [tag()] };

const PROPOSALS: NewDedupLabelProposal[] = [
  {
    image_id: 101, model: MODEL, label: 'interier - kuchyne',
    confidence: 0.87, proposed_at: '2026-08-06T00:00:00Z', status: 'pending',
    reviewed_at: null, reviewed_by: null, current_state: null,
  },
];

function stateResult(over: Partial<NewDedupProposalStateResult> = {}): NewDedupProposalStateResult {
  return {
    image_id: 101, model: MODEL, label: 'interier - kuchyne', state: 'positive',
    status: 'confirmed', proposed_label: 'interier - kuchyne', corrected: false,
    ...over,
  };
}

const IMAGE: ImagePublic = {
  id: 101, sreality_id: 555, sequence: 1, sreality_url: 'https://sdn.cz/x.jpg',
  storage_path: 'img/555/1.jpg', clip_fine_tag: 'kitchen', clip_logical_tag: 'kitchen',
  clip_confidence: 0.6, clip_render_score: 0.1, phash: 123,
};

function tagImage(over: Partial<NewDedupTagImage> = {}): NewDedupTagImage {
  return {
    image_id: 101, storage_path: 'img/555/1.jpg', state: 'untouched',
    updated_at: null, created_by: null,
    ...over,
  };
}

/* TAG_STATES' order IS the button order inside a TriStateControl — pinned by
 * "renders three state buttons…" below, so every other test can index. */
const STATE_ORDER: ReadonlyArray<TagState> = ['positive', 'negative', 'excluded'];

const stateGroups = () => screen.getAllByRole('group', { name: 'Tag state' });
const stateBtn = (group: HTMLElement, state: TagState) =>
  within(group).getAllByRole('button')[STATE_ORDER.indexOf(state)];
const setStateOn = (index: number, state: TagState) =>
  fireEvent.click(stateBtn(stateGroups()[index], state));
/* Which tile the grid's keyboard cursor is on: the focused tile is the one
 * whose tri-state buttons carry the focus ring. */
const focusedTile = () =>
  stateGroups().findIndex((g) => g.querySelector('.ring-2') != null);

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <NewDedupLabeling />
    </QueryClientProvider>,
  );
}

const grid = (container: HTMLElement) =>
  container.querySelector('[style*="--tile-min"]') as HTMLElement;

describe('<NewDedupLabeling>', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // The taxonomy chart's collapsed state persists in localStorage — without
    // this, one test's collapse leaks into every test that follows.
    localStorage.clear();
    vi.mocked(api.getNewDedupLabelingOverview).mockResolvedValue({ data: OVERVIEW });
    vi.mocked(api.listNewDedupSettings).mockResolvedValue({ data: SETTINGS });
    vi.mocked(api.listNewDedupProposals).mockResolvedValue({ data: PROPOSALS });
    vi.mocked(api.listNewDedupOriginalTags).mockResolvedValue({
      data: ['bathroom', 'kitchen', 'living_room'],
    });
    vi.mocked(api.listNewDedupTagImages).mockResolvedValue({ data: [] });
    vi.mocked(api.listNewDedupImageTags).mockResolvedValue({ data: [] });
    vi.mocked(api.listNewDedupPositiveTagsForImages).mockResolvedValue({ data: [] });
    vi.mocked(queries.fetchImagesByImageIds).mockResolvedValue(new Map([[101, IMAGE]]));
    vi.mocked(queries.fetchBorderCasesByImageIds).mockResolvedValue(new Set());
    vi.mocked(api.setBorderCase).mockResolvedValue({
      data: { image_id: 101, created_at: '2026-08-21T00:00:00Z' },
    });
    vi.mocked(api.deleteBorderCase).mockResolvedValue({ data: { deleted: true } });
  });

  // --- taxonomy chart ------------------------------------------------------

  it('renders the taxonomy bar chart sorted by gate count, most first', async () => {
    vi.mocked(api.getNewDedupLabelingOverview).mockResolvedValue({
      data: {
        sample_size: 42,
        tags: [
          tag({ id: 1, label: 'interier - kuchyne', positive_count: 12, gate_count: 12 }),
          tag({ id: 2, label: 'exterier - fasada', positive_count: 54, gate_count: 54,
            pending_count: 0, negative_count: 0, excluded_count: 0 }),
          tag({ id: 3, label: 'garaz', positive_count: 4, gate_count: 4,
            pending_count: 0, negative_count: 0, excluded_count: 0 }),
        ],
      },
    });
    renderPage();
    const bars = await screen.findAllByRole(
      'button', { name: /^(interier - kuchyne|exterier - fasada|garaz)$/ },
    );
    expect(bars.map((b) => b.textContent)).toEqual([
      'exterier - fasada', 'interier - kuchyne', 'garaz',
    ]);
    // Value at the bar tip, and the pending annotation for the one tag with any.
    expect(screen.getByText('54')).toBeInTheDocument();
    expect(screen.getByText('12')).toBeInTheDocument();
    expect(screen.getByText('4')).toBeInTheDocument();
    expect(screen.getByText('· 3/300 pending')).toBeInTheDocument();
  });

  it('reports the negative and excluded counts beside the positive bar', async () => {
    renderPage();
    // The bar is positives only, but a tag whose negatives are all defaulted is
    // a different dataset from one that was worked through — the matrix's whole
    // point. Both other states have to be visible without opening anything.
    expect(await screen.findByText('· 8 neg · 5 excl')).toBeInTheDocument();
  });

  it('shows a priority tag\'s bar and label in red, unless it is the active filter', async () => {
    vi.mocked(api.getNewDedupLabelingOverview).mockResolvedValue({
      data: {
        sample_size: 42,
        tags: [
          tag({ id: 1, label: 'interier - kuchyne', priority: true }),
          tag({ id: 2, label: 'exterier - fasada', priority: false,
            pending_count: 0, negative_count: 0, excluded_count: 0 }),
        ],
      },
    });
    renderPage();
    const priorityBtn = await screen.findByRole('button', { name: 'interier - kuchyne' });
    const otherBtn = screen.getByRole('button', { name: 'exterier - fasada' });
    expect(priorityBtn).toHaveClass('text-[var(--color-brick)]');
    expect(otherBtn).not.toHaveClass('text-[var(--color-brick)]');
    const priorityBar = priorityBtn.closest('div')!.nextElementSibling!.querySelector('[aria-hidden]')!;
    expect(priorityBar).toHaveClass('bg-[var(--color-brick)]');

    // Filtering to it takes precedence over the priority color — the active
    // highlight is the more immediate "you're looking at this now" signal.
    fireEvent.click(priorityBtn);
    await waitFor(() => expect(priorityBtn).toHaveClass('text-[var(--color-copper)]'));
    expect(priorityBtn).not.toHaveClass('text-[var(--color-brick)]');
  });

  it('measures Gate 1 on the unparked images, not the whole positive set', async () => {
    vi.mocked(api.getNewDedupLabelingOverview).mockResolvedValue({
      data: {
        sample_size: 42,
        tags: [tag({ positive_count: 150, gate_count: 110, border_case_count: 40 })],
      },
    });
    renderPage();
    // 150 images are positive, but 40 are parked as border cases: the tag is at
    // 110 of Gate 1's 150, NOT at target. Showing 150 would call it done on
    // images nobody could classify.
    expect(await screen.findByText('110')).toBeInTheDocument();
    expect(screen.queryByText('150')).not.toBeInTheDocument();
    expect(screen.getByText('· 40 parked')).toBeInTheDocument();
  });

  it('narrows the coverage ceiling by the gate count, not the raw positive total', async () => {
    vi.mocked(api.getNewDedupLabelingOverview).mockResolvedValue({
      data: {
        sample_size: 42,
        tags: [
          tag({ id: 1, label: 'mostly parked',
            positive_count: 200, gate_count: 20, border_case_count: 180 }),
          tag({ id: 2, label: 'genuinely covered',
            positive_count: 200, gate_count: 200, border_case_count: 0 }),
        ],
      },
    });
    renderPage();
    await screen.findByRole('button', { name: 'genuinely covered' });
    // "which tags are still short" has to mean short of the GATE — a tag whose
    // 200 positives are 180 border cases still needs work.
    fireEvent.change(screen.getByLabelText('Max training images per tag'), {
      target: { value: '50' },
    });
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'genuinely covered' })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole('button', { name: 'mostly parked' })).toBeInTheDocument();
  });

  it('collapses the taxonomy chart, keeping its header (and the coverage filter) reachable', async () => {
    renderPage();
    expect(await screen.findByRole('button', { name: 'interier - kuchyne' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { expanded: true }));

    expect(screen.queryByRole('button', { name: 'interier - kuchyne' })).not.toBeInTheDocument();
    // Folded, but not gone: the header and the tag-coverage ceiling it carries
    // stay usable, and the tag count is still readable.
    expect(screen.getByRole('button', { expanded: false })).toBeInTheDocument();
    expect(screen.getByLabelText('Max training images per tag')).toBeInTheDocument();
    expect(screen.getByText(/Taxonomy v1 \(1 tags/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { expanded: false }));
    expect(await screen.findByRole('button', { name: 'interier - kuchyne' })).toBeInTheDocument();
  });

  // --- taxonomy management -------------------------------------------------

  it('opens the manage modal and adds a new tag', async () => {
    vi.mocked(api.addNewDedupTag).mockResolvedValue({
      data: tag({ id: 2, label: 'exterier - fasada', family: null,
        positive_count: 0, gate_count: 0, negative_count: 0, excluded_count: 0,
        pending_count: 0, dismissed_count: 0 }),
    });
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    fireEvent.click(screen.getByText('Modify labels'));
    const input = await screen.findByPlaceholderText(/new label/);
    fireEvent.change(input, { target: { value: 'exterier - fasada' } });
    fireEvent.click(screen.getByText('Add label'));
    await waitFor(() => expect(api.addNewDedupTag).toHaveBeenCalledWith('exterier - fasada'));
  });

  it('renames a tag from the manage modal', async () => {
    vi.mocked(api.renameNewDedupTag).mockResolvedValue({
      data: tag({ label: 'interier - kuchyn nova' }),
    });
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    fireEvent.click(screen.getByText('Modify labels'));
    fireEvent.click(await screen.findByText('rename'));
    // Scope to the modal: the proposal tiles carry their own tag pickers,
    // seeded with this same label text.
    const modal = within(screen.getByRole('dialog', { name: /Modify Taxonomy/ }));
    fireEvent.change(modal.getByDisplayValue('interier - kuchyne'), {
      target: { value: 'interier - kuchyn nova' },
    });
    fireEvent.click(modal.getByText('Save'));
    await waitFor(() =>
      expect(api.renameNewDedupTag).toHaveBeenCalledWith(1, 'interier - kuchyn nova'),
    );
  });

  it('removes a tag after a two-step confirm that counts every annotation state', async () => {
    vi.mocked(api.removeNewDedupTag).mockResolvedValue({
      data: { label: 'interier - kuchyne', deleted_annotations: 25 },
    });
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    fireEvent.click(screen.getByText('Modify labels'));
    fireEvent.click(await screen.findByLabelText('Remove interier - kuchyne'));
    expect(api.removeNewDedupTag).not.toHaveBeenCalled();
    // The warning has to name what actually goes: every (image, tag) cell under
    // this tag, not just the positive ones (12 + 8 negative + 5 excluded).
    expect(screen.getByText(/25 annotations go with it/)).toBeInTheDocument();
    fireEvent.click(screen.getByText('Remove'));
    await waitFor(() => expect(api.removeNewDedupTag).toHaveBeenCalledWith(1));
  });

  it('pins a priority tag to the top of the manage modal, marked in red', async () => {
    const TWO: NewDedupLabelingOverview = {
      sample_size: 42,
      tags: [
        tag({ id: 1, label: 'aaa - first alphabetically' }),
        tag({ id: 2, label: 'zzz - last alphabetically', priority: true }),
      ],
    };
    vi.mocked(api.getNewDedupLabelingOverview).mockResolvedValue({ data: TWO });
    renderPage();
    await screen.findByRole('button', { name: 'aaa - first alphabetically' });
    fireEvent.click(screen.getByText('Modify labels'));
    const modal = within(await screen.findByRole('dialog', { name: /Modify Taxonomy/ }));
    const names = modal.getAllByTitle(/alphabetically/).map((el) => el.textContent);
    // Priority pins to the top even though it sorts last alphabetically.
    expect(names).toEqual(['zzz - last alphabetically', 'aaa - first alphabetically']);
    expect(modal.getByText('zzz - last alphabetically')).toHaveClass('text-[var(--color-brick)]');
    expect(modal.getByText('aaa - first alphabetically')).not.toHaveClass('text-[var(--color-brick)]');
  });

  it('toggles a tag\'s priority and ready-for-training flags from the manage modal', async () => {
    vi.mocked(api.setNewDedupTagFlags).mockResolvedValue({ data: tag({ priority: true }) });
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    fireEvent.click(screen.getByText('Modify labels'));
    const modal = within(await screen.findByRole('dialog', { name: /Modify Taxonomy/ }));

    fireEvent.click(modal.getByRole('button', { name: 'Priority' }));
    await waitFor(() =>
      expect(api.setNewDedupTagFlags).toHaveBeenCalledWith(1, { priority: true }),
    );

    fireEvent.click(modal.getByRole('button', { name: 'Ready for training' }));
    await waitFor(() =>
      expect(api.setNewDedupTagFlags).toHaveBeenCalledWith(1, { ready_for_training: true }),
    );
  });

  it('clearing a flag sends an explicit false, not an omitted field', async () => {
    vi.mocked(api.getNewDedupLabelingOverview).mockResolvedValue({
      data: { sample_size: 42, tags: [tag({ priority: true })] },
    });
    vi.mocked(api.setNewDedupTagFlags).mockResolvedValue({ data: tag({ priority: false }) });
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    fireEvent.click(screen.getByText('Modify labels'));
    const modal = within(await screen.findByRole('dialog', { name: /Modify Taxonomy/ }));

    fireEvent.click(modal.getByRole('button', { name: 'Priority' }));
    await waitFor(() =>
      expect(api.setNewDedupTagFlags).toHaveBeenCalledWith(1, { priority: false }),
    );
  });

  it('renaming an unrelated tag in the manage modal never hijacks the active filter', async () => {
    const TWO: NewDedupLabelingOverview = {
      sample_size: 42,
      tags: [
        tag(),
        tag({ id: 2, label: 'koupelna', family: null, positive_count: 0, gate_count: 0,
          negative_count: 0, excluded_count: 0, pending_count: 0, dismissed_count: 0 }),
      ],
    };
    vi.mocked(api.getNewDedupLabelingOverview).mockResolvedValue({ data: TWO });
    vi.mocked(api.renameNewDedupTag).mockResolvedValue({
      data: { ...TWO.tags[1], label: 'koupelna-v2' },
    });
    renderPage();
    // Filter proposals to "interier - kuchyne" via the bar chart first.
    fireEvent.click(await screen.findByRole('button', { name: 'interier - kuchyne' }));
    await waitFor(() =>
      expect(api.listNewDedupProposals).toHaveBeenCalledWith(
        expect.objectContaining({ label: 'interier - kuchyne' }),
      ),
    );

    fireEvent.click(screen.getByText('Modify labels'));
    const koupelnaRow = (await screen.findByLabelText('Remove koupelna')).closest('div')!
      .parentElement!.parentElement!;
    fireEvent.click(within(koupelnaRow).getByText('rename'));
    fireEvent.change(within(koupelnaRow).getByDisplayValue('koupelna'), {
      target: { value: 'koupelna-v2' },
    });
    fireEvent.click(within(koupelnaRow).getByText('Save'));
    await waitFor(() => expect(api.renameNewDedupTag).toHaveBeenCalledWith(2, 'koupelna-v2'));

    // The filter must still read "interier - kuchyne" — no call to
    // listNewDedupProposals with label 'koupelna-v2' should ever happen.
    expect((screen.getByLabelText('Tag') as HTMLSelectElement).value).toBe('interier - kuchyne');
    expect(api.listNewDedupProposals).not.toHaveBeenCalledWith(
      expect.objectContaining({ label: 'koupelna-v2' }),
    );
  });

  it('removing the currently-filtered tag from the manage modal clears the filter', async () => {
    vi.mocked(api.removeNewDedupTag).mockResolvedValue({
      data: { label: 'interier - kuchyne', deleted_annotations: 25 },
    });
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'interier - kuchyne' }));
    await waitFor(() =>
      expect((screen.getByLabelText('Tag') as HTMLSelectElement).value).toBe('interier - kuchyne'),
    );

    fireEvent.click(screen.getByText('Modify labels'));
    fireEvent.click(await screen.findByLabelText('Remove interier - kuchyne'));
    fireEvent.click(screen.getByText('Remove'));
    await waitFor(() => expect(api.removeNewDedupTag).toHaveBeenCalledWith(1));
    expect((screen.getByLabelText('Tag') as HTMLSelectElement).value).toBe('');
  });

  // --- growing the sample --------------------------------------------------

  it('grows the sample with the entered count', async () => {
    vi.mocked(api.growNewDedupSample).mockResolvedValue({ data: { added: 50 } });
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    fireEvent.change(screen.getByDisplayValue('200'), { target: { value: '50' } });
    fireEvent.click(screen.getByText('Grow sample'));
    await waitFor(() => expect(api.growNewDedupSample).toHaveBeenCalledWith(50, null));
  });

  it('shows an inline pending state while growing, so a slow request never reads as "nothing happened"', async () => {
    let resolveGrow: (v: { data: { added: number } }) => void = () => {};
    vi.mocked(api.growNewDedupSample).mockImplementation(
      () => new Promise((resolve) => { resolveGrow = resolve; }),
    );
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    fireEvent.click(screen.getByText('Grow sample'));

    expect(await screen.findByText('Growing…')).toBeInTheDocument();
    expect(screen.getByDisplayValue('200')).toBeDisabled();

    resolveGrow({ data: { added: 1000 } });
    await waitFor(() => expect(screen.getByText(/Added 1000 images/)).toBeInTheDocument());
    await waitFor(() => expect(screen.queryByText('Growing…')).not.toBeInTheDocument());
  });

  it('tells the operator when a grow finds nothing new to add', async () => {
    vi.mocked(api.growNewDedupSample).mockResolvedValue({ data: { added: 0 } });
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    fireEvent.click(screen.getByText('Grow sample'));
    await waitFor(() => expect(screen.getByText(/No new images matched/)).toBeInTheDocument());
  });

  // --- the tri-state control ----------------------------------------------

  it('renders three state buttons per tile, with untouched shown as a defaulted negative', async () => {
    renderPage();
    const group = (await screen.findAllByRole('group', { name: 'Tag state' }))[0];
    const buttons = within(group).getAllByRole('button');
    // Exactly three, in TAG_STATES order — one control, never two widgets for
    // "is it positive" and "is it excluded".
    expect(buttons).toHaveLength(3);
    expect(buttons.map((b) => b.getAttribute('title'))).toEqual([
      expect.stringMatching(/^Positive/),
      expect.stringMatching(/^Negative/),
      expect.stringMatching(/^Excluded/),
    ]);
    // Untouched: nothing is pressed, and the negative slot is dashed to say
    // "this is the default, not a decision".
    expect(buttons.map((b) => b.getAttribute('aria-pressed'))).toEqual(['false', 'false', 'false']);
    expect(buttons[1].className).toContain('border-dashed');
    expect(buttons[1].getAttribute('title')).toMatch(/defaulted/);
  });

  it('names the tag the tri-state control is deciding, right above the buttons', async () => {
    // The buttons act on ONE tag (the proposal's own label, or a correction
    // typed into the picker) — never every tag on the image. Naming it right
    // above the controls removes the ambiguity a bare row of buttons has.
    renderPage();
    await screen.findAllByRole('group', { name: 'Tag state' });
    expect(screen.getByTitle('Setting the state of "interier - kuchyne"')).toHaveTextContent(
      'interier - kuchyne',
    );

    // Correcting the tag before deciding updates the label too — the
    // combobox only commits a typed correction on blur.
    const picker = screen.getByPlaceholderText('tag…');
    fireEvent.change(picker, { target: { value: 'garaz' } });
    fireEvent.blur(picker);
    expect(screen.getByTitle('Setting the state of "garaz"')).toBeInTheDocument();
  });

  it('marks the decided state as pressed once the row carries one', async () => {
    vi.mocked(api.listNewDedupProposals).mockResolvedValue({
      data: [{ ...PROPOSALS[0], status: 'confirmed', reviewed_by: 'operator',
        current_state: 'positive' }],
    });
    renderPage();
    fireEvent.click(await screen.findByRole('tab', { name: 'Confirmed' }));
    const group = (await screen.findAllByRole('group', { name: 'Tag state' }))[0];
    expect(within(group).getAllByRole('button').map((b) => b.getAttribute('aria-pressed')))
      .toEqual(['true', 'false', 'false']);
  });

  it('sets a proposal positive and drops that tile from Pending WITHOUT refetching the grid', async () => {
    vi.mocked(api.setNewDedupProposalState).mockResolvedValue({ data: stateResult() });
    renderPage();
    await screen.findAllByRole('group', { name: 'Tag state' });
    const callsBefore = vi.mocked(api.listNewDedupProposals).mock.calls.length;

    setStateOn(0, 'positive');

    await waitFor(() =>
      // An uncorrected decision sends NO label — the server then uses the
      // proposal's own stored label, so a rename landing between page load and
      // click can't be undone by echoing back a stale spelling.
      expect(api.setNewDedupProposalState).toHaveBeenCalledWith(101, MODEL, 'positive', undefined),
    );
    // The decided tile leaves this tab — but by a local cache patch, not a
    // refetch. Refetching re-renders (and, on tied proposed_at values,
    // re-orders) every remaining tile: the churn this page is built to avoid.
    await waitFor(() => expect(screen.getByText('No pending proposals.')).toBeInTheDocument());
    expect(vi.mocked(api.listNewDedupProposals).mock.calls.length).toBe(callsBefore);
  });

  it('sets a proposal excluded, which is a dismissal for bookkeeping but "excluded" in the matrix', async () => {
    vi.mocked(api.setNewDedupProposalState).mockResolvedValue({
      data: stateResult({ state: 'excluded', status: 'dismissed' }),
    });
    renderPage();
    await screen.findAllByRole('group', { name: 'Tag state' });

    setStateOn(0, 'excluded');

    // "excluded" must reach the annotation write intact — collapsing it into
    // the proposal's dismissed/confirmed bookkeeping would lose the one state
    // that means "drop this cell from training", not "this tag is wrong".
    await waitFor(() =>
      expect(api.setNewDedupProposalState).toHaveBeenCalledWith(101, MODEL, 'excluded', undefined),
    );
  });

  it('sets a proposal negative from the same control', async () => {
    vi.mocked(api.setNewDedupProposalState).mockResolvedValue({
      data: stateResult({ state: 'negative', status: 'dismissed' }),
    });
    renderPage();
    await screen.findAllByRole('group', { name: 'Tag state' });
    setStateOn(0, 'negative');
    await waitFor(() =>
      expect(api.setNewDedupProposalState).toHaveBeenCalledWith(101, MODEL, 'negative', undefined),
    );
  });

  it('decides against a corrected tag when the suggestion is wrong', async () => {
    vi.mocked(api.setNewDedupProposalState).mockResolvedValue({
      data: stateResult({ label: 'interier - loznice', corrected: true }),
    });
    renderPage();
    // The tile's picker is seeded with the suggestion; typing a different
    // taxonomy label and deciding must send THAT, not the suggestion.
    const picker = await screen.findByPlaceholderText('tag…');
    fireEvent.change(picker, { target: { value: 'interier - loznice' } });
    fireEvent.blur(picker);
    expect(await screen.findByText(/will decide against/)).toBeInTheDocument();

    setStateOn(0, 'positive');
    await waitFor(() =>
      expect(api.setNewDedupProposalState).toHaveBeenCalledWith(
        101, MODEL, 'positive', 'interier - loznice',
      ),
    );
  });

  it('leaves every other tile untouched when one is decided on Pending', async () => {
    vi.mocked(api.setNewDedupProposalState).mockResolvedValue({ data: stateResult() });
    vi.mocked(api.listNewDedupProposals).mockResolvedValue({
      data: [
        PROPOSALS[0],
        { ...PROPOSALS[0], image_id: 102, label: 'exterier - fasada' },
        { ...PROPOSALS[0], image_id: 103, label: 'garaz' },
      ],
    });
    vi.mocked(queries.fetchImagesByImageIds).mockResolvedValue(
      new Map([[101, IMAGE], [102, { ...IMAGE, id: 102 }], [103, { ...IMAGE, id: 103 }]]),
    );
    renderPage();
    await waitFor(() => expect(screen.getAllByPlaceholderText('tag…')).toHaveLength(3));
    const imageCallsBefore = vi.mocked(queries.fetchImagesByImageIds).mock.calls.length;

    setStateOn(0, 'positive');

    await waitFor(() => expect(screen.getAllByPlaceholderText('tag…')).toHaveLength(2));
    // Order preserved, and no image re-fetch: the photo cache accumulates by id
    // instead of being keyed on the current (now changed) id list.
    expect(
      screen.getAllByPlaceholderText('tag…').map((i) => (i as HTMLInputElement).value),
    ).toEqual(['exterier - fasada', 'garaz']);
    expect(vi.mocked(queries.fetchImagesByImageIds).mock.calls.length).toBe(imageCallsBefore);
  });

  it('re-deciding an already-decided proposal overwrites it in place', async () => {
    // The old relabel-in-place path ("Save tag" on a reviewed tile) is gone —
    // the tri-state control is live on every status instead, and the server
    // has no pending-only guard (there's only one write path into
    // image_tag_labels here, so a repeat call can't diverge anything).
    vi.mocked(api.listNewDedupProposals).mockResolvedValue({
      data: [{ ...PROPOSALS[0], status: 'confirmed', reviewed_by: 'operator',
        current_state: 'positive' }],
    });
    vi.mocked(api.setNewDedupProposalState).mockResolvedValue({
      data: {
        image_id: 101, model: MODEL, label: PROPOSALS[0].label, state: 'negative',
        status: 'dismissed', proposed_label: PROPOSALS[0].label, corrected: false,
      },
    });
    renderPage();
    fireEvent.click(await screen.findByRole('tab', { name: 'Confirmed' }));
    await screen.findAllByRole('group', { name: 'Tag state' });

    setStateOn(0, 'negative');

    await waitFor(() =>
      expect(api.setNewDedupProposalState).toHaveBeenCalledWith(101, MODEL, 'negative', undefined),
    );
    // Stays on the Confirmed tab (only Pending drops a reviewed row) with its
    // new state reflected — a real overwrite, not a fake local change.
    await waitFor(() =>
      expect(stateBtn(stateGroups()[0], 'negative')).toHaveAttribute('aria-pressed', 'true'),
    );
  });

  // --- the batch bar -------------------------------------------------------

  it('offers one batch button per state and sets the selection through the proposal endpoint', async () => {
    vi.mocked(api.bulkSetNewDedupProposalState).mockResolvedValue({
      data: { updated: 1, model: MODEL, state: 'positive', image_ids: [101] },
    });
    renderPage();
    await screen.findByText('Select all');
    expect(screen.getByText('Set selected: positive')).toBeInTheDocument();
    expect(screen.getByText('Set selected: negative')).toBeInTheDocument();
    expect(screen.getByText('Set selected: excluded')).toBeInTheDocument();

    const callsBefore = vi.mocked(api.listNewDedupProposals).mock.calls.length;
    fireEvent.click(screen.getByText('Select all'));
    fireEvent.click(screen.getByText('Set selected: positive'));

    await waitFor(() =>
      expect(api.bulkSetNewDedupProposalState).toHaveBeenCalledWith(MODEL, [101], 'positive'),
    );
    await waitFor(() => expect(screen.getByText('No pending proposals.')).toBeInTheDocument());
    expect(vi.mocked(api.listNewDedupProposals).mock.calls.length).toBe(callsBefore);
  });

  it('takes a corrected tile out of the batch so a bulk set cannot discard the fix', async () => {
    // Two pending proposals; select both, then correct the first one's tag. The
    // batch endpoint writes each proposal's OWN label, so a corrected tile must
    // drop out of the selection rather than be silently decided under the
    // model's label.
    vi.mocked(api.listNewDedupProposals).mockResolvedValue({
      data: [PROPOSALS[0], { ...PROPOSALS[0], image_id: 102, label: 'exterier - fasada' }],
    });
    vi.mocked(api.bulkSetNewDedupProposalState).mockResolvedValue({
      data: { updated: 1, model: MODEL, state: 'positive', image_ids: [102] },
    });
    renderPage();
    fireEvent.click(await screen.findByText('Select all'));
    expect(screen.getByText('2 selected')).toBeInTheDocument();

    const picker = screen.getAllByPlaceholderText('tag…')[0];
    fireEvent.change(picker, { target: { value: 'interier - loznice' } });
    fireEvent.blur(picker);

    await waitFor(() => expect(screen.getByText('1 selected')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Set selected: positive'));
    await waitFor(() =>
      expect(api.bulkSetNewDedupProposalState).toHaveBeenCalledWith(MODEL, [102], 'positive'),
    );
  });

  it('only batches rows from the CURRENT secondary model', async () => {
    vi.mocked(api.listNewDedupProposals).mockResolvedValue({
      data: [PROPOSALS[0], { ...PROPOSALS[0], image_id: 102, model: 'older-model' }],
    });
    vi.mocked(api.bulkSetNewDedupProposalState).mockResolvedValue({
      data: { updated: 1, model: MODEL, state: 'negative', image_ids: [101] },
    });
    renderPage();
    fireEvent.click(await screen.findByText('Select all'));
    // An older model's leftover pending rows still review one at a time — the
    // batch endpoint takes ONE model.
    expect(screen.getByText('1 selected')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Set selected: negative'));
    await waitFor(() =>
      expect(api.bulkSetNewDedupProposalState).toHaveBeenCalledWith(MODEL, [101], 'negative'),
    );
  });

  // --- drafts + the tag picker --------------------------------------------

  it('keeps per-model drafts separate for the same image', async () => {
    // One image, two models' proposals — correcting one must not rewrite the
    // other (label_proposals' PK is (image_id, model), so both are real rows).
    vi.mocked(api.listNewDedupProposals).mockResolvedValue({
      data: [PROPOSALS[0], { ...PROPOSALS[0], model: 'older-model', label: 'exterier - fasada' }],
    });
    renderPage();
    const pickers = await screen.findAllByPlaceholderText('tag…');
    expect(pickers).toHaveLength(2);

    fireEvent.change(pickers[0], { target: { value: 'interier - loznice' } });
    fireEvent.blur(pickers[0]);

    await waitFor(() => expect((pickers[0] as HTMLInputElement).value).toBe('interier - loznice'));
    expect((pickers[1] as HTMLInputElement).value).toBe('exterier - fasada');
  });

  it('keeps the tag dropdown un-clipped — no overflow-hidden ancestor inside the card', async () => {
    renderPage();
    fireEvent.focus(await screen.findByPlaceholderText('tag…'));
    const listbox = await screen.findByRole('listbox');

    // The dropdown is absolutely positioned and overflows the card by design.
    // An `overflow-hidden` anywhere up the chain silently clips it to nothing
    // (jsdom does no layout, so only the class can be asserted — but that IS
    // the bug: the card used to carry overflow-hidden to round the photo).
    const offenders: string[] = [];
    for (let el = listbox.parentElement; el && el !== document.body; el = el.parentElement) {
      if (el.className.includes('overflow-hidden')) offenders.push(el.className);
    }
    expect(offenders).toEqual([]);
  });

  it('renders the tag picker after the state control so its dropdown cannot cover it', async () => {
    renderPage();
    const picker = await screen.findByPlaceholderText('tag…');
    const positive = stateBtn(stateGroups()[0], 'positive');
    // The dropdown opens downward out of the picker. If the picker preceded the
    // tri-state buttons in document order it would paint over them and eat the
    // first click aimed at one (LabelCombobox keeps focus on mousedown, so the
    // option, not the button, receives it).
    expect(
      picker.compareDocumentPosition(positive) & Node.DOCUMENT_POSITION_PRECEDING,
    ).toBeTruthy();
  });

  it('leaves the per-tile tag picker offering the WHOLE taxonomy, ceiling or not', async () => {
    vi.mocked(api.getNewDedupLabelingOverview).mockResolvedValue({
      data: {
        sample_size: 42,
        tags: [
          tag({ id: 1, label: 'interier - kuchyne', family: null,
            positive_count: 12, gate_count: 12 }),
          tag({ id: 2, label: 'exterier - fasada', family: null,
            positive_count: 200, gate_count: 200 }),
        ],
      },
    });
    renderPage();
    await screen.findByRole('button', { name: 'exterier - fasada' });
    fireEvent.change(screen.getByLabelText('Max training images per tag'), {
      target: { value: '50' },
    });

    // The ceiling picks what to WORK ON; it must never restrict what a wrong
    // suggestion can be corrected to.
    fireEvent.focus(await screen.findByPlaceholderText('tag…'));
    const options = within(await screen.findByRole('listbox')).getAllByRole('option');
    expect(options.map((o) => o.textContent)).toEqual(
      expect.arrayContaining([
        expect.stringContaining('exterier - fasada'),
        expect.stringContaining('interier - kuchyne'),
      ]),
    );
  });

  // --- badges, lightbox, image size ---------------------------------------

  it('shows the proposed tag by default and switches to the original on toggle', async () => {
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    // Default view = "New tag": the proposal's label wins the badge.
    expect(await screen.findByText('interier - kuchyne', { selector: 'span' })).toBeInTheDocument();
    fireEvent.click(screen.getByText('Original tag'));
    // Original = image.clip_fine_tag ('kitchen') via imageTagLabel's Czech map.
    await waitFor(() => expect(screen.getByText('kuchyně')).toBeInTheDocument());
  });

  it('enlarges a tile in the shared lightbox, badged with the tag the tile itself shows', async () => {
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Open photo 101' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('1 / 1')).toBeInTheDocument();
    // The grid is on "New tag", so the enlarged photo must carry the PROPOSED
    // tag — not the image row's own CLIP call ('kitchen' → 'kuchyně'), which is
    // what the lightbox shows by default everywhere else in the app.
    expect(within(dialog).getByText('interier - kuchyne')).toBeInTheDocument();
    expect(within(dialog).queryByText('kuchyně')).not.toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('badges the enlarged photo with the original CLIP tag on the "Original tag" view', async () => {
    renderPage();
    await screen.findByRole('button', { name: 'Open photo 101' });
    fireEvent.click(screen.getByText('Original tag'));
    fireEvent.click(screen.getByRole('button', { name: 'Open photo 101' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('kuchyně')).toBeInTheDocument();
  });

  // --- filtering by the original CLIP tag ----------------------------------

  it('offers the CLIP tagger\'s own vocabulary in the Tag dropdown on "Original tag"', async () => {
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    fireEvent.click(screen.getByText('Original tag'));
    const select = screen.getByLabelText('Tag') as HTMLSelectElement;
    await waitFor(() =>
      expect([...select.options].map((o) => o.textContent)).toEqual([
        'All original tags', 'bathroom', 'kitchen', 'living_room',
      ]),
    );
  });

  it('filters proposals by the original tag, not the Taxonomy v1 label', async () => {
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    fireEvent.click(screen.getByText('Original tag'));
    await screen.findByRole('option', { name: 'kitchen' });
    const callsBefore = vi.mocked(api.listNewDedupProposals).mock.calls.length;

    fireEvent.change(screen.getByLabelText('Tag'), { target: { value: 'kitchen' } });

    await waitFor(() =>
      expect(api.listNewDedupProposals).toHaveBeenCalledWith(
        expect.objectContaining({ original_tag: 'kitchen', label: undefined }),
      ),
    );
    expect(vi.mocked(api.listNewDedupProposals).mock.calls.length).toBe(callsBefore + 1);
  });

  it('toggling New/Original alone never refetches the grid', async () => {
    // A display-only toggle (which badge shows) must not blink the grid —
    // the same invariant the page holds everywhere else.
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    const callsBefore = vi.mocked(api.listNewDedupProposals).mock.calls.length;

    fireEvent.click(screen.getByText('Original tag'));
    fireEvent.click(screen.getByText('New tag'));

    expect(vi.mocked(api.listNewDedupProposals).mock.calls.length).toBe(callsBefore);
  });

  it('switching to Original tag drops an active Taxonomy v1 filter and does refetch', async () => {
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'interier - kuchyne' }));
    await waitFor(() =>
      expect(api.listNewDedupProposals).toHaveBeenCalledWith(
        expect.objectContaining({ label: 'interier - kuchyne' }),
      ),
    );

    fireEvent.click(screen.getByText('Original tag'));
    await waitFor(() =>
      expect(api.listNewDedupProposals).toHaveBeenLastCalledWith(
        expect.objectContaining({ label: undefined, original_tag: undefined }),
      ),
    );
  });

  it('remembers each vocabulary\'s own filter independently across toggles', async () => {
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'interier - kuchyne' }));
    await waitFor(() =>
      expect((screen.getByLabelText('Tag') as HTMLSelectElement).value).toBe('interier - kuchyne'),
    );

    fireEvent.click(screen.getByText('Original tag'));
    await screen.findByRole('option', { name: 'kitchen' });
    fireEvent.change(screen.getByLabelText('Tag'), { target: { value: 'kitchen' } });
    await waitFor(() =>
      expect((screen.getByLabelText('Tag') as HTMLSelectElement).value).toBe('kitchen'),
    );

    fireEvent.click(screen.getByText('New tag'));
    expect((screen.getByLabelText('Tag') as HTMLSelectElement).value).toBe('interier - kuchyne');
  });

  it('walks the whole grid from the lightbox, in tile order', async () => {
    vi.mocked(api.listNewDedupProposals).mockResolvedValue({
      data: [PROPOSALS[0], { ...PROPOSALS[0], image_id: 102, label: 'exterier - fasada' }],
    });
    vi.mocked(queries.fetchImagesByImageIds).mockResolvedValue(
      new Map([[101, IMAGE], [102, { ...IMAGE, id: 102 }]]),
    );
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Open photo 101' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('1 / 2')).toBeInTheDocument();

    fireEvent.keyDown(document, { key: 'ArrowRight' });
    await waitFor(() => expect(within(dialog).getByText('2 / 2')).toBeInTheDocument());
    // Each stop carries its OWN tile's proposed tag — the gallery is parallel to
    // the grid, not a bag of images.
    expect(within(dialog).getByText('exterier - fasada')).toBeInTheDocument();
  });

  it('opens at the clicked tile even when an earlier tile has no photo yet', async () => {
    // 101's photo is still in flight, so it is not in the gallery at all — the
    // index passed to the lightbox is a position in the gallery, never in the
    // proposals list, or clicking 102 would enlarge someone else.
    vi.mocked(api.listNewDedupProposals).mockResolvedValue({
      data: [PROPOSALS[0], { ...PROPOSALS[0], image_id: 102, label: 'exterier - fasada' }],
    });
    vi.mocked(queries.fetchImagesByImageIds).mockResolvedValue(
      new Map([[102, { ...IMAGE, id: 102 }]]),
    );
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Open photo 102' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('1 / 1')).toBeInTheDocument();
    expect(within(dialog).getByText('exterier - fasada')).toBeInTheDocument();
  });

  it('resizes the review grid from the shared small/large switch, and remembers the choice', async () => {
    const { container, unmount } = renderPage();
    await screen.findByRole('button', { name: 'Open photo 101' });

    // jsdom does no layout, so the assertable contract is the one custom
    // property the grid's track floor is defined by — the same single
    // definition point Browse's cards use (CARD_IMAGE_MIN / --card-min).
    expect(grid(container).style.getPropertyValue('--tile-min')).toBe('14rem');

    fireEvent.click(screen.getByRole('button', { name: /Large/ }));
    await waitFor(() => expect(grid(container).style.getPropertyValue('--tile-min')).toBe('28rem'));

    // A workspace preference, not part of the view — it survives a reload
    // rather than resetting every time the page is opened.
    unmount();
    const second = renderPage();
    await screen.findByRole('button', { name: 'Open photo 101' });
    expect(grid(second.container).style.getPropertyValue('--tile-min')).toBe('28rem');
  });

  it("keeps the image-size choice out of Browse's own preference", async () => {
    renderPage();
    await screen.findByRole('button', { name: 'Open photo 101' });
    fireEvent.click(screen.getByRole('button', { name: /Large/ }));
    await waitFor(() =>
      expect(localStorage.getItem('sreality.newDedupLabeling.imageLarge')).toBe('1'),
    );
    // Sizing tiles here must never reshape the listing cards on Browse.
    expect(localStorage.getItem('sreality.browse.cardImageLarge')).toBeNull();
  });

  // --- assigned tags, shown below the image --------------------------------

  it('shows the tags already assigned to an image below its photo', async () => {
    vi.mocked(api.listNewDedupPositiveTagsForImages).mockResolvedValue({
      data: [
        { image_id: 101, tag_id: 1, label: 'interier - kuchyne' },
        { image_id: 101, tag_id: 7, label: 'exterier - fasada' },
      ],
    });
    renderPage();
    await screen.findByRole('button', { name: 'Open photo 101' });
    const list = await screen.findByRole('list', { name: 'Assigned tags' });
    expect(within(list).getByText('interier - kuchyne')).toBeInTheDocument();
    expect(within(list).getByText('exterier - fasada')).toBeInTheDocument();
    // One batched call for the whole visible grid, not one per tile.
    expect(vi.mocked(api.listNewDedupPositiveTagsForImages).mock.calls).toHaveLength(1);
    expect(api.listNewDedupPositiveTagsForImages).toHaveBeenCalledWith([101]);
  });

  it('renders nothing under an untouched image, to keep the tile clean', async () => {
    renderPage();
    await screen.findByRole('button', { name: 'Open photo 101' });
    expect(screen.queryByRole('list', { name: 'Assigned tags' })).not.toBeInTheDocument();
  });

  it('adds a tile\'s own tag to its assigned-tags row the moment it is set positive', async () => {
    // On Pending, deciding a tile drops it from view — the All tab is what
    // patches in place, so it's the one that can show the row updating.
    vi.mocked(api.listNewDedupProposals).mockResolvedValue({ data: [PROPOSALS[0]] });
    vi.mocked(api.setNewDedupProposalState).mockResolvedValue({ data: stateResult() });
    renderPage();
    fireEvent.click(await screen.findByRole('tab', { name: 'All' }));
    await screen.findAllByRole('group', { name: 'Tag state' });
    expect(screen.queryByRole('list', { name: 'Assigned tags' })).not.toBeInTheDocument();

    setStateOn(0, 'positive');
    await waitFor(() => expect(api.setNewDedupProposalState).toHaveBeenCalled());

    // Patched from the mutation response, not a refetch of the batch endpoint.
    const list = await screen.findByRole('list', { name: 'Assigned tags' });
    expect(within(list).getByText('interier - kuchyne')).toBeInTheDocument();
    expect(vi.mocked(api.listNewDedupPositiveTagsForImages).mock.calls).toHaveLength(1);
  });

  it('drops a tag from the assigned-tags row when it is set back off positive', async () => {
    vi.mocked(api.listNewDedupPositiveTagsForImages).mockResolvedValue({
      data: [{ image_id: 101, tag_id: 1, label: 'interier - kuchyne' }],
    });
    vi.mocked(api.listNewDedupProposals).mockResolvedValue({
      data: [{ ...PROPOSALS[0], status: 'confirmed', current_state: 'positive' }],
    });
    vi.mocked(api.setNewDedupProposalState).mockResolvedValue({
      data: {
        image_id: 101, model: MODEL, label: PROPOSALS[0].label, state: 'negative',
        status: 'dismissed', proposed_label: PROPOSALS[0].label, corrected: false,
      },
    });
    renderPage();
    fireEvent.click(await screen.findByRole('tab', { name: 'Confirmed' }));
    await screen.findByRole('list', { name: 'Assigned tags' });

    setStateOn(0, 'negative');
    await waitFor(() =>
      expect(api.setNewDedupProposalState).toHaveBeenCalledWith(101, MODEL, 'negative', undefined),
    );
    await waitFor(() =>
      expect(screen.queryByRole('list', { name: 'Assigned tags' })).not.toBeInTheDocument(),
    );
  });

  it('shows assigned tags in Sample mode tiles too', async () => {
    vi.mocked(api.listNewDedupTagImages).mockResolvedValue({ data: [tagImage()] });
    vi.mocked(api.listNewDedupPositiveTagsForImages).mockResolvedValue({
      data: [{ image_id: 101, tag_id: 1, label: 'interier - kuchyne' }],
    });
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    fireEvent.click(screen.getByRole('button', { name: 'Sample' }));
    fireEvent.change(screen.getByLabelText('Tag'), { target: { value: 'interier - kuchyne' } });
    const list = await screen.findByRole('list', { name: 'Assigned tags' });
    expect(within(list).getByText('interier - kuchyne')).toBeInTheDocument();
  });

  it('updates the assigned-tags row from the detail panel', async () => {
    vi.mocked(api.listNewDedupImageTags).mockResolvedValue({
      data: [
        { id: 7, label: 'interier - obyvak', family: 'interier', state: 'untouched', updated_at: null },
      ],
    });
    vi.mocked(api.setNewDedupTagAnnotation).mockResolvedValue({
      data: { image_id: 101, tag_id: 7, state: 'positive', updated_at: 't' },
    });
    renderPage();
    await screen.findByRole('button', { name: 'Open photo 101' });
    expect(screen.queryByRole('list', { name: 'Assigned tags' })).not.toBeInTheDocument();

    fireEvent.click(await screen.findByText('all tags'));
    const panel = await screen.findByRole('dialog', { name: 'All tags on this image' });
    const group = await within(panel).findByRole('group', { name: 'Tag state' });
    fireEvent.click(stateBtn(group, 'positive'));

    await waitFor(() => expect(api.setNewDedupTagAnnotation).toHaveBeenCalledWith(7, 101, 'positive'));
    fireEvent.keyDown(window, { key: 'Escape' });
    const list = await screen.findByRole('list', { name: 'Assigned tags' });
    expect(within(list).getByText('interier - obyvak')).toBeInTheDocument();
  });

  // --- border cases (image-grain, independent of every tag's state) --------

  it('parks a tile as a border case without deciding it or refetching the grid', async () => {
    renderPage();
    await screen.findAllByRole('group', { name: 'Tag state' });
    const callsBefore = vi.mocked(api.listNewDedupProposals).mock.calls.length;

    fireEvent.click(screen.getByRole('button', { name: 'Border case' }));
    await waitFor(() => expect(api.setBorderCase).toHaveBeenCalledWith(101));

    // A border case is not a verdict: the proposal is still pending, the tile
    // keeps its tri-state control and its place in the grid, and nothing
    // refetched.
    expect(await screen.findByRole('button', { name: '✓ Border case' })).toBeInTheDocument();
    expect(stateGroups()).toHaveLength(1);
    expect(api.setNewDedupProposalState).not.toHaveBeenCalled();
    expect(vi.mocked(api.listNewDedupProposals).mock.calls.length).toBe(callsBefore);
  });

  it('clears a flag the server already had, from the same button', async () => {
    vi.mocked(queries.fetchBorderCasesByImageIds).mockResolvedValue(new Set([101]));
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: '✓ Border case' }));
    await waitFor(() => expect(api.deleteBorderCase).toHaveBeenCalledWith(101));
    expect(api.setBorderCase).not.toHaveBeenCalled();
  });

  // --- keyboard review -----------------------------------------------------

  it('moves the keyboard cursor with the arrow keys and with j/k', async () => {
    vi.mocked(api.listNewDedupProposals).mockResolvedValue({
      data: [
        PROPOSALS[0],
        { ...PROPOSALS[0], image_id: 102, label: 'exterier - fasada' },
        { ...PROPOSALS[0], image_id: 103, label: 'garaz' },
      ],
    });
    const { container } = renderPage();
    await waitFor(() => expect(stateGroups()).toHaveLength(3));
    // Nothing is focused until a key (or a click/hover) puts the cursor
    // somewhere — the grid must not steal a decision on the first keystroke.
    expect(focusedTile()).toBe(-1);

    fireEvent.keyDown(grid(container), { key: 'k' });
    expect(focusedTile()).toBe(0);

    fireEvent.keyDown(grid(container), { key: 'ArrowRight' });
    expect(focusedTile()).toBe(1);

    fireEvent.keyDown(grid(container), { key: 'j' });
    expect(focusedTile()).toBe(2);

    fireEvent.keyDown(grid(container), { key: 'ArrowLeft' });
    expect(focusedTile()).toBe(1);
  });

  it('sets the focused tile from a hotkey and advances to the next one', async () => {
    // On the All tab a decided tile stays in place, so the cursor's advance is
    // observable — that is the whole "assign, next image" loop this exists for.
    vi.mocked(api.listNewDedupProposals).mockResolvedValue({
      data: [
        PROPOSALS[0],
        { ...PROPOSALS[0], image_id: 102, label: 'exterier - fasada' },
        { ...PROPOSALS[0], image_id: 103, label: 'garaz' },
      ],
    });
    vi.mocked(api.setNewDedupProposalState).mockResolvedValue({ data: stateResult() });
    const { container } = renderPage();
    fireEvent.click(await screen.findByRole('tab', { name: 'All' }));
    await waitFor(() => expect(stateGroups()).toHaveLength(3));

    fireEvent.keyDown(grid(container), { key: 'k' });
    expect(focusedTile()).toBe(0);

    fireEvent.keyDown(grid(container), { key: '1' });
    await waitFor(() =>
      expect(api.setNewDedupProposalState).toHaveBeenCalledWith(101, MODEL, 'positive', undefined),
    );
    expect(focusedTile()).toBe(1);

    fireEvent.keyDown(grid(container), { key: '3' });
    await waitFor(() =>
      expect(api.setNewDedupProposalState).toHaveBeenCalledWith(102, MODEL, 'excluded', undefined),
    );
  });

  it('accepts p and x as letter aliases for positive and excluded', async () => {
    vi.mocked(api.listNewDedupProposals).mockResolvedValue({
      data: [PROPOSALS[0], { ...PROPOSALS[0], image_id: 102, label: 'exterier - fasada' }],
    });
    vi.mocked(api.setNewDedupProposalState).mockResolvedValue({ data: stateResult() });
    const { container } = renderPage();
    fireEvent.click(await screen.findByRole('tab', { name: 'All' }));
    await waitFor(() => expect(stateGroups()).toHaveLength(2));

    fireEvent.keyDown(grid(container), { key: 'k' });
    fireEvent.keyDown(grid(container), { key: 'x' });
    await waitFor(() =>
      expect(api.setNewDedupProposalState).toHaveBeenCalledWith(101, MODEL, 'excluded', undefined),
    );

    // 'x' advanced the cursor onto the second tile, so 'p' decides THAT one —
    // the same one-key-per-image loop the digits give.
    fireEvent.keyDown(grid(container), { key: 'p' });
    await waitFor(() =>
      expect(api.setNewDedupProposalState).toHaveBeenCalledWith(102, MODEL, 'positive', undefined),
    );
  });

  it('sends the corrected tag when a hotkey decides a tile whose suggestion was fixed', async () => {
    vi.mocked(api.setNewDedupProposalState).mockResolvedValue({
      data: stateResult({ label: 'interier - loznice', corrected: true }),
    });
    const { container } = renderPage();
    const picker = await screen.findByPlaceholderText('tag…');
    fireEvent.change(picker, { target: { value: 'interier - loznice' } });
    fireEvent.blur(picker);
    await screen.findByText(/will decide against/);

    fireEvent.keyDown(grid(container), { key: 'k' });
    fireEvent.keyDown(grid(container), { key: '1' });
    // A correction typed into the picker must survive the keyboard path too, or
    // the fast loop would quietly overwrite it with the model's own guess.
    await waitFor(() =>
      expect(api.setNewDedupProposalState).toHaveBeenCalledWith(
        101, MODEL, 'positive', 'interier - loznice',
      ),
    );
  });

  // --- Sample mode ---------------------------------------------------------

  it('asks for a tag before browsing the sample', async () => {
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    fireEvent.click(screen.getByRole('button', { name: 'Sample' }));
    // The whole point of this mode is "every image in the pool for ONE tag" —
    // there is no meaningful unscoped listing, so it asks instead of guessing.
    expect(await screen.findByText('Choose a tag above to browse its sample.')).toBeInTheDocument();
    expect(api.listNewDedupTagImages).not.toHaveBeenCalled();
  });

  it('browses the whole labeling sample for one tag, untouched first', async () => {
    vi.mocked(api.listNewDedupTagImages).mockResolvedValue({
      data: [tagImage(), tagImage({ image_id: 102 })],
    });
    vi.mocked(queries.fetchImagesByImageIds).mockResolvedValue(
      new Map([[101, IMAGE], [102, { ...IMAGE, id: 102 }]]),
    );
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    fireEvent.click(screen.getByRole('button', { name: 'Sample' }));
    fireEvent.change(screen.getByLabelText('Tag'), { target: { value: 'interier - kuchyne' } });

    // Scoped by the tag's ID, not its label text — the reason tag_taxonomy got
    // a surrogate key in the first place.
    await waitFor(() =>
      expect(api.listNewDedupTagImages).toHaveBeenCalledWith(
        1, { state: 'untouched', limit: 200 },
      ),
    );
    await waitFor(() => expect(stateGroups()).toHaveLength(2));
    // The proposal queue's controls have no meaning here: the tag is fixed by
    // the filter, so there is nothing to correct.
    expect(screen.queryByPlaceholderText('tag…')).not.toBeInTheDocument();
  });

  it('sets one image\'s state for the browsed tag, and it leaves a filtered view', async () => {
    vi.mocked(api.listNewDedupTagImages).mockResolvedValue({
      data: [tagImage(), tagImage({ image_id: 102 })],
    });
    vi.mocked(queries.fetchImagesByImageIds).mockResolvedValue(
      new Map([[101, IMAGE], [102, { ...IMAGE, id: 102 }]]),
    );
    vi.mocked(api.setNewDedupTagAnnotation).mockResolvedValue({
      data: { image_id: 101, tag_id: 1, state: 'positive', updated_at: 't' },
    });
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    fireEvent.click(screen.getByRole('button', { name: 'Sample' }));
    fireEvent.change(screen.getByLabelText('Tag'), { target: { value: 'interier - kuchyne' } });
    await waitFor(() => expect(stateGroups()).toHaveLength(2));

    setStateOn(0, 'positive');

    // Straight to image_tag_labels — no proposal row involved, so no pending
    // guard and no 404 on a re-decide.
    await waitFor(() => expect(api.setNewDedupTagAnnotation).toHaveBeenCalledWith(1, 101, 'positive'));
    // The view is "untouched only", so a now-positive tile no longer belongs in
    // it — patched out in place, never by refetching the grid.
    await waitFor(() => expect(stateGroups()).toHaveLength(1));
    expect(vi.mocked(api.listNewDedupTagImages).mock.calls).toHaveLength(1);
  });

  it('re-filters the sample by state', async () => {
    vi.mocked(api.listNewDedupTagImages).mockResolvedValue({
      data: [tagImage({ state: 'excluded' })],
    });
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    fireEvent.click(screen.getByRole('button', { name: 'Sample' }));
    fireEvent.change(screen.getByLabelText('Tag'), { target: { value: 'interier - kuchyne' } });
    await waitFor(() => expect(api.listNewDedupTagImages).toHaveBeenCalled());

    fireEvent.change(screen.getByLabelText('State'), { target: { value: 'excluded' } });
    // "show me every image where kitchen = excluded" is the query this mode
    // exists to answer, and it has to reach images no model ever proposed the
    // tag for.
    await waitFor(() =>
      expect(api.listNewDedupTagImages).toHaveBeenCalledWith(1, { state: 'excluded', limit: 200 }),
    );
    const group = (await screen.findAllByRole('group', { name: 'Tag state' }))[0];
    expect(within(group).getAllByRole('button').map((b) => b.getAttribute('aria-pressed')))
      .toEqual(['false', 'false', 'true']);
  });

  it('drops the state filter entirely on "All"', async () => {
    vi.mocked(api.listNewDedupTagImages).mockResolvedValue({ data: [tagImage()] });
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    fireEvent.click(screen.getByRole('button', { name: 'Sample' }));
    fireEvent.change(screen.getByLabelText('Tag'), { target: { value: 'interier - kuchyne' } });
    await waitFor(() => expect(api.listNewDedupTagImages).toHaveBeenCalled());

    fireEvent.change(screen.getByLabelText('State'), { target: { value: 'all' } });
    await waitFor(() =>
      expect(api.listNewDedupTagImages).toHaveBeenCalledWith(1, { state: undefined, limit: 200 }),
    );
  });

  it('batch-sets a whole sample screen through the tag-annotation endpoint', async () => {
    vi.mocked(api.listNewDedupTagImages).mockResolvedValue({
      data: [tagImage(), tagImage({ image_id: 102 })],
    });
    vi.mocked(api.bulkSetNewDedupTagAnnotation).mockResolvedValue({
      data: { updated: 2, tag_id: 1, state: 'negative', image_ids: [101, 102] },
    });
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    fireEvent.click(screen.getByRole('button', { name: 'Sample' }));
    fireEvent.change(screen.getByLabelText('Tag'), { target: { value: 'interier - kuchyne' } });
    await waitFor(() => expect(stateGroups()).toHaveLength(2));

    fireEvent.click(screen.getByText('Select all'));
    expect(screen.getByText('2 selected')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Set selected: negative'));

    // The batch bar means the same three things in both modes, but it must
    // route to the TAG endpoint here — the proposal one would 404 on images
    // that have no proposal row at all.
    await waitFor(() =>
      expect(api.bulkSetNewDedupTagAnnotation).toHaveBeenCalledWith(1, [101, 102], 'negative'),
    );
    expect(api.bulkSetNewDedupProposalState).not.toHaveBeenCalled();
  });

  it('sets a sample tile from the keyboard too', async () => {
    vi.mocked(api.listNewDedupTagImages).mockResolvedValue({ data: [tagImage()] });
    vi.mocked(api.setNewDedupTagAnnotation).mockResolvedValue({
      data: { image_id: 101, tag_id: 1, state: 'excluded', updated_at: 't' },
    });
    const { container } = renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    fireEvent.click(screen.getByRole('button', { name: 'Sample' }));
    fireEvent.change(screen.getByLabelText('Tag'), { target: { value: 'interier - kuchyne' } });
    await waitFor(() => expect(stateGroups()).toHaveLength(1));

    fireEvent.keyDown(grid(container), { key: 'k' });
    fireEvent.keyDown(grid(container), { key: '3' });
    await waitFor(() =>
      expect(api.setNewDedupTagAnnotation).toHaveBeenCalledWith(1, 101, 'excluded'),
    );
  });

  it('tells the operator when a tag has no images in the chosen state', async () => {
    vi.mocked(api.listNewDedupTagImages).mockResolvedValue({ data: [] });
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    fireEvent.click(screen.getByRole('button', { name: 'Sample' }));
    fireEvent.change(screen.getByLabelText('Tag'), { target: { value: 'interier - kuchyne' } });
    expect(await screen.findByText('No images match.')).toBeInTheDocument();
  });

  // --- the all-tags detail panel ------------------------------------------

  it('opens every active tag on one image, grouped by family', async () => {
    vi.mocked(api.listNewDedupImageTags).mockResolvedValue({
      data: [
        { id: 1, label: 'interier - kuchyne', family: 'interier', state: 'positive', updated_at: 't' },
        { id: 2, label: 'interier - obyvak', family: 'interier', state: 'untouched', updated_at: null },
        { id: 3, label: 'exterier - fasada', family: 'exterier', state: 'negative', updated_at: 't' },
      ] satisfies NewDedupImageTag[],
    });
    renderPage();
    fireEvent.click(await screen.findByText('all tags'));

    const panel = await screen.findByRole('dialog', { name: 'All tags on this image' });
    await waitFor(() => expect(api.listNewDedupImageTags).toHaveBeenCalledWith(101));
    expect(within(panel).getByText('Image 101 — all tags')).toBeInTheDocument();
    // Grouped by family — "open kitchen-living room" is one sitting across
    // several tags, not a hunt through per-tag screens.
    expect(within(panel).getByText('interier')).toBeInTheDocument();
    expect(within(panel).getByText('exterier')).toBeInTheDocument();
    expect(within(panel).getAllByRole('group', { name: 'Tag state' })).toHaveLength(3);
  });

  it('sets one tag\'s state from the detail panel via the plain annotation endpoint', async () => {
    vi.mocked(api.listNewDedupImageTags).mockResolvedValue({
      data: [
        { id: 7, label: 'interier - obyvak', family: 'interier', state: 'positive', updated_at: 't' },
      ],
    });
    vi.mocked(api.setNewDedupTagAnnotation).mockResolvedValue({
      data: { image_id: 101, tag_id: 7, state: 'excluded', updated_at: 't2' },
    });
    renderPage();
    fireEvent.click(await screen.findByText('all tags'));
    const panel = await screen.findByRole('dialog', { name: 'All tags on this image' });
    const group = await within(panel).findByRole('group', { name: 'Tag state' });

    fireEvent.click(stateBtn(group, 'excluded'));

    // The panel writes image_tag_labels directly (it has a real tag_id already,
    // no proposal row involved), never through the proposal endpoint.
    await waitFor(() => expect(api.setNewDedupTagAnnotation).toHaveBeenCalledWith(7, 101, 'excluded'));
    expect(api.setNewDedupProposalState).not.toHaveBeenCalled();
    // Patched in place — the panel doesn't blink through a refetch.
    await waitFor(() =>
      expect(within(group).getAllByRole('button').map((b) => b.getAttribute('aria-pressed')))
        .toEqual(['false', 'false', 'true']),
    );
    expect(vi.mocked(api.listNewDedupImageTags).mock.calls).toHaveLength(1);
  });

  it('selects all untouched tags and sets them negative in one action', async () => {
    // The fitness-room case: a handful of tags decided one at a time, the
    // rest closed out explicitly instead of left as an implicit default.
    vi.mocked(api.listNewDedupImageTags).mockResolvedValue({
      data: [
        { id: 1, label: 'interier - kuchyne', family: 'interier', state: 'positive', updated_at: 't' },
        { id: 2, label: 'interier - obyvak', family: 'interier', state: 'untouched', updated_at: null },
        { id: 3, label: 'exterier - fasada', family: 'exterier', state: 'untouched', updated_at: null },
      ],
    });
    vi.mocked(api.bulkSetNewDedupImageTags).mockResolvedValue({
      data: { updated: 2, image_id: 101, state: 'negative', tag_ids: [2, 3] },
    });
    renderPage();
    fireEvent.click(await screen.findByText('all tags'));
    const panel = await screen.findByRole('dialog', { name: 'All tags on this image' });
    await within(panel).findAllByRole('group', { name: 'Tag state' });

    // The already-positive tag is not offered by "select all" — only the
    // two untouched ones are, so it can never be silently overwritten.
    fireEvent.click(within(panel).getByRole('button', { name: 'Select all untouched' }));
    expect(within(panel).getByText('2 selected')).toBeInTheDocument();
    expect(
      within(panel).getByLabelText('Select interier - kuchyne for batch action'),
    ).not.toBeChecked();
    expect(
      within(panel).getByLabelText('Select interier - obyvak for batch action'),
    ).toBeChecked();

    fireEvent.click(within(panel).getByRole('button', { name: 'Set selected: negative' }));
    await waitFor(() =>
      expect(api.bulkSetNewDedupImageTags).toHaveBeenCalledWith(101, [2, 3], 'negative'),
    );
    // Patched in place, and the selection clears once applied.
    expect(within(panel).queryByText('2 selected')).not.toBeInTheDocument();
    const groups = within(panel).getAllByRole('group', { name: 'Tag state' });
    expect(stateBtn(groups[1], 'negative')).toHaveAttribute('aria-pressed', 'true');
    expect(stateBtn(groups[2], 'negative')).toHaveAttribute('aria-pressed', 'true');
    expect(vi.mocked(api.listNewDedupImageTags).mock.calls).toHaveLength(1);
  });

  it('drops a tag from the selection once it is decided individually', async () => {
    vi.mocked(api.listNewDedupImageTags).mockResolvedValue({
      data: [
        { id: 1, label: 'a', family: null, state: 'untouched', updated_at: null },
        { id: 2, label: 'b', family: null, state: 'untouched', updated_at: null },
      ],
    });
    vi.mocked(api.setNewDedupTagAnnotation).mockResolvedValue({
      data: { image_id: 101, tag_id: 1, state: 'positive', updated_at: 't' },
    });
    renderPage();
    fireEvent.click(await screen.findByText('all tags'));
    const panel = await screen.findByRole('dialog', { name: 'All tags on this image' });
    const groups = await within(panel).findAllByRole('group', { name: 'Tag state' });

    fireEvent.click(within(panel).getByRole('button', { name: 'Select all untouched' }));
    expect(within(panel).getByText('2 selected')).toBeInTheDocument();

    fireEvent.click(stateBtn(groups[0], 'positive'));
    await waitFor(() => expect(api.setNewDedupTagAnnotation).toHaveBeenCalledWith(1, 101, 'positive'));

    // Tag "a" is no longer untouched — it must not still be in the batch.
    expect(within(panel).getByText('1 selected')).toBeInTheDocument();
    expect(within(panel).getByLabelText('Select a for batch action')).not.toBeChecked();
    expect(within(panel).getByLabelText('Select b for batch action')).toBeChecked();
  });

  it('closes the detail panel on Escape', async () => {
    renderPage();
    fireEvent.click(await screen.findByText('all tags'));
    await screen.findByRole('dialog', { name: 'All tags on this image' });
    fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'All tags on this image' })).not.toBeInTheDocument(),
    );
  });

  // --- tabs + tag filter ---------------------------------------------------

  it('switching status tabs re-queries proposals with the new status', async () => {
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    fireEvent.click(screen.getByRole('tab', { name: 'Confirmed' }));
    await waitFor(() =>
      expect(api.listNewDedupProposals).toHaveBeenCalledWith(
        expect.objectContaining({ status: 'confirmed' }),
      ),
    );
  });

  it('filters the grid to one tag from the tag select', async () => {
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    fireEvent.change(screen.getByLabelText('Tag'), { target: { value: 'interier - kuchyne' } });
    await waitFor(() =>
      expect(api.listNewDedupProposals).toHaveBeenCalledWith(
        expect.objectContaining({ label: 'interier - kuchyne', status: 'pending' }),
      ),
    );
  });

  it('narrows both the chart and the tag select to tags still short of training images', async () => {
    vi.mocked(api.getNewDedupLabelingOverview).mockResolvedValue({
      data: {
        sample_size: 42,
        tags: [
          tag({ id: 1, label: 'interier - kuchyne', family: null,
            positive_count: 12, gate_count: 12, pending_count: 0 }),
          tag({ id: 2, label: 'exterier - fasada', family: null,
            positive_count: 200, gate_count: 200, pending_count: 0 }),
        ],
      },
    });
    renderPage();
    await screen.findByRole('button', { name: 'exterier - fasada' });

    fireEvent.change(screen.getByLabelText('Max training images per tag'), {
      target: { value: '50' },
    });

    // The well-covered tag drops out of the chart AND out of the tag select —
    // the point of the filter is to work through what's still short of Gate 1.
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'exterier - fasada' })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole('button', { name: 'interier - kuchyne' })).toBeInTheDocument();
    const tagSelect = screen.getByLabelText('Tag') as HTMLSelectElement;
    expect([...tagSelect.options].map((o) => o.textContent)).toEqual([
      'All tags', 'interier - kuchyne (12)',
    ]);
    // Said twice on purpose: once in the chart header, once beside the select.
    expect(screen.getByText(/1 of 2 tags \(≤ 50 training images\)/)).toBeInTheDocument();
    expect(screen.getByText(/Taxonomy v1 \(1 of 2 tags/)).toBeInTheDocument();
  });

  it('never hides the tag the grid is currently filtered to, whatever the ceiling', async () => {
    vi.mocked(api.getNewDedupLabelingOverview).mockResolvedValue({
      data: {
        sample_size: 42,
        tags: [tag({ id: 2, label: 'exterier - fasada', family: null,
          positive_count: 200, gate_count: 200 })],
      },
    });
    renderPage();
    // Wait for the taxonomy to land, else the select has no option to pick yet.
    await screen.findByRole('button', { name: 'exterier - fasada' });
    fireEvent.change(screen.getByLabelText('Tag'), { target: { value: 'exterier - fasada' } });
    fireEvent.change(screen.getByLabelText('Max training images per tag'), {
      target: { value: '10' },
    });
    // Otherwise the select would read "All tags" while the grid stayed filtered.
    expect((screen.getByLabelText('Tag') as HTMLSelectElement).value).toBe('exterier - fasada');
  });

  // --- the All tab ---------------------------------------------------------

  const MIXED: NewDedupLabelProposal[] = [
    { ...PROPOSALS[0], image_id: 101, label: 'interier - kuchyne', status: 'pending',
      current_state: null },
    { ...PROPOSALS[0], image_id: 102, label: 'exterier - fasada', status: 'confirmed',
      reviewed_by: 'operator', current_state: 'positive' },
    { ...PROPOSALS[0], image_id: 103, label: 'garaz', status: 'dismissed',
      reviewed_by: 'operator', current_state: 'excluded' },
  ];

  it('greys out the already-handled tiles on All and leaves the pending one bright', async () => {
    vi.mocked(api.listNewDedupProposals).mockResolvedValue({ data: MIXED });
    vi.mocked(queries.fetchImagesByImageIds).mockResolvedValue(
      new Map([[101, IMAGE], [102, { ...IMAGE, id: 102 }], [103, { ...IMAGE, id: 103 }]]),
    );
    renderPage();
    fireEvent.click(await screen.findByRole('tab', { name: 'All' }));
    await waitFor(() =>
      expect(api.listNewDedupProposals).toHaveBeenCalledWith(
        expect.objectContaining({ status: 'all' }),
      ),
    );

    // One tile still awaits a decision; the decided ones recede.
    await waitFor(() => expect(document.querySelectorAll('[data-dimmed]')).toHaveLength(2));
    // The bright one is the pending row — the only tile with nothing pressed.
    const pressed = stateGroups().map((g) =>
      within(g).getAllByRole('button').some((b) => b.getAttribute('aria-pressed') === 'true'),
    );
    expect(pressed).toEqual([false, true, true]);
  });

  it('deciding on All greys the tile in place instead of moving or reordering anything', async () => {
    vi.mocked(api.listNewDedupProposals).mockResolvedValue({ data: MIXED });
    vi.mocked(api.setNewDedupProposalState).mockResolvedValue({ data: stateResult() });
    vi.mocked(queries.fetchImagesByImageIds).mockResolvedValue(
      new Map([[101, IMAGE], [102, { ...IMAGE, id: 102 }], [103, { ...IMAGE, id: 103 }]]),
    );
    renderPage();
    fireEvent.click(await screen.findByRole('tab', { name: 'All' }));
    await waitFor(() => expect(stateGroups()).toHaveLength(3));
    const callsBefore = vi.mocked(api.listNewDedupProposals).mock.calls.length;

    setStateOn(0, 'positive');

    // Same three tiles, same order, one more greyed and no refetch: this tab is
    // where the operator works continuously, so nothing may move.
    await waitFor(() => expect(document.querySelectorAll('[data-dimmed]')).toHaveLength(3));
    expect(vi.mocked(api.listNewDedupProposals).mock.calls.length).toBe(callsBefore);
    expect(
      screen.getAllByPlaceholderText('tag…').map((i) => (i as HTMLInputElement).value),
    ).toEqual(['interier - kuchyne', 'exterier - fasada', 'garaz']);
  });

  it('drops a per-tile-decided image out of the batch selection on All', async () => {
    // The tile stays on screen there, so a stale selection would re-send an id
    // that is no longer pending on the next batch action.
    vi.mocked(api.listNewDedupProposals).mockResolvedValue({
      data: [MIXED[0], { ...MIXED[0], image_id: 104, label: 'garaz' }],
    });
    vi.mocked(api.setNewDedupProposalState).mockResolvedValue({ data: stateResult() });
    renderPage();
    fireEvent.click(await screen.findByRole('tab', { name: 'All' }));
    fireEvent.click(await screen.findByText('Select all'));
    expect(screen.getByText('2 selected')).toBeInTheDocument();

    setStateOn(0, 'positive');

    await waitFor(() => expect(screen.getByText('1 selected')).toBeInTheDocument());
  });

  it('offers the batch bar for pending rows on the All tab too', async () => {
    vi.mocked(api.listNewDedupProposals).mockResolvedValue({ data: MIXED });
    renderPage();
    fireEvent.click(await screen.findByRole('tab', { name: 'All' }));
    // Exactly the one pending row is selectable — batch select is scoped to
    // fresh suggestions on purpose; re-deciding an already-decided one is a
    // deliberate one-at-a-time action through its own tri-state control.
    fireEvent.click(await screen.findByText('Select all'));
    expect(screen.getByText('1 selected')).toBeInTheDocument();
  });
});
