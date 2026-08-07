/* NewDedupLabeling — the Wave 1 Labeling page.
 *
 * Hermetic: mock every /new-dedup/labeling api.ts call, listNewDedupSettings,
 * and the Supabase image read (fetchImagesByImageIds). Pins: taxonomy rows
 * render with their counts, add/rename/remove call the right endpoint, the
 * new-vs-original toggle swaps which tag the badge shows, per-tile
 * confirm/dismiss fire, and the batch bar only enables for the current
 * secondary model. Backend CRUD is covered by
 * tests/api/test_new_dedup_labeling_routes.py; this only checks the wiring.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import NewDedupLabeling from './NewDedupLabeling';
import type {
  NewDedupConfirmResult,
  NewDedupLabelingOverview,
  NewDedupLabelProposal,
  NewDedupSetting,
} from '@/lib/api';
import * as api from '@/lib/api';
import * as queries from '@/lib/queries';
import type { ImagePublic } from '@/lib/types';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    getNewDedupLabelingOverview: vi.fn(),
    addNewDedupTaxonomyLabel: vi.fn(),
    renameNewDedupTaxonomyLabel: vi.fn(),
    removeNewDedupTaxonomyLabel: vi.fn(),
    growNewDedupSample: vi.fn(),
    listNewDedupProposals: vi.fn(),
    confirmNewDedupProposal: vi.fn(),
    dismissNewDedupProposal: vi.fn(),
    bulkConfirmNewDedupProposals: vi.fn(),
    bulkDismissNewDedupProposals: vi.fn(),
    listNewDedupSettings: vi.fn(),
    setTrainingExample: vi.fn(),
  };
});

vi.mock('@/lib/queries', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/queries')>();
  return { ...actual, fetchImagesByImageIds: vi.fn() };
});

const SETTINGS: NewDedupSetting[] = [
  {
    key: 'labeling_secondary_model', category: 'labeling', value_type: 'text',
    value: 'openai/clip-vit-large-patch14', default: 'openai/clip-vit-large-patch14',
    is_override: false, decided: false, explanation: 'The stronger CLIP checkpoint.',
    enum_choices: null, minimum: null, maximum: null,
  },
  {
    key: 'labeling_target_proposals_per_category', category: 'labeling', value_type: 'integer',
    value: 300, default: 300, is_override: false, decided: true,
    explanation: 'How many proposals a label needs.',
    enum_choices: null, minimum: 1, maximum: null,
  },
  {
    key: 'labeling_gate1_target_per_tag', category: 'labeling', value_type: 'integer',
    value: 150, default: 150, is_override: false, decided: true,
    explanation: 'Wave 1 gate.', enum_choices: null, minimum: 1, maximum: null,
  },
];

const OVERVIEW: NewDedupLabelingOverview = {
  sample_size: 42,
  labels: [
    {
      id: 1, label: 'interier - kuchyne', family: 'interier', active: true,
      created_at: '2026-08-01T00:00:00Z', confirmed_count: 12, pending_count: 3,
      dismissed_count: 1,
    },
  ],
};

const PROPOSALS: NewDedupLabelProposal[] = [
  {
    image_id: 101, model: 'openai/clip-vit-large-patch14', label: 'interier - kuchyne',
    confidence: 0.87, proposed_at: '2026-08-06T00:00:00Z', status: 'pending',
    reviewed_at: null, reviewed_by: null, trained_label: null,
  },
];

const CONFIRM_RESULT: NewDedupConfirmResult = {
  image_id: 101, model: 'openai/clip-vit-large-patch14', label: 'interier - kuchyne',
  status: 'confirmed', proposed_label: 'interier - kuchyne', corrected: false,
};

const IMAGE: ImagePublic = {
  id: 101, sreality_id: 555, sequence: 1, sreality_url: 'https://sdn.cz/x.jpg',
  storage_path: 'img/555/1.jpg', clip_fine_tag: 'kitchen', clip_logical_tag: 'kitchen',
  clip_confidence: 0.6, clip_render_score: 0.1, phash: 123,
};

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

describe('<NewDedupLabeling>', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // The taxonomy chart's collapsed state persists in localStorage — without
    // this, one test's collapse leaks into every test that follows.
    localStorage.clear();
    vi.mocked(api.getNewDedupLabelingOverview).mockResolvedValue({ data: OVERVIEW });
    vi.mocked(api.listNewDedupSettings).mockResolvedValue({ data: SETTINGS });
    vi.mocked(api.listNewDedupProposals).mockResolvedValue({ data: PROPOSALS });
    vi.mocked(queries.fetchImagesByImageIds).mockResolvedValue(
      new Map([[101, IMAGE]]),
    );
  });

  it('renders the taxonomy bar chart sorted by confirmed count, most first', async () => {
    vi.mocked(api.getNewDedupLabelingOverview).mockResolvedValue({
      data: {
        sample_size: 42,
        labels: [
          { id: 1, label: 'interier - kuchyne', family: 'interier', active: true,
            created_at: 't', confirmed_count: 12, pending_count: 3, dismissed_count: 1 },
          { id: 2, label: 'exterier - fasada', family: null, active: true,
            created_at: 't', confirmed_count: 54, pending_count: 0, dismissed_count: 0 },
          { id: 3, label: 'garaz', family: null, active: true,
            created_at: 't', confirmed_count: 4, pending_count: 0, dismissed_count: 0 },
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
    // Value at the bar tip, and the pending annotation for the one label that has any.
    expect(screen.getByText('54')).toBeInTheDocument();
    expect(screen.getByText('12')).toBeInTheDocument();
    expect(screen.getByText('4')).toBeInTheDocument();
    expect(screen.getByText('· 3/300 pending')).toBeInTheDocument();
  });

  it('opens the manage modal and adds a new taxonomy label', async () => {
    vi.mocked(api.addNewDedupTaxonomyLabel).mockResolvedValue({
      data: {
        id: 2, label: 'exterier - fasada', family: null, active: true, created_at: 't',
        confirmed_count: 0, pending_count: 0, dismissed_count: 0,
      },
    });
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    fireEvent.click(screen.getByText('Modify labels'));
    const input = await screen.findByPlaceholderText(/new label/);
    fireEvent.change(input, { target: { value: 'exterier - fasada' } });
    fireEvent.click(screen.getByText('Add label'));
    await waitFor(() =>
      expect(api.addNewDedupTaxonomyLabel).toHaveBeenCalledWith('exterier - fasada'),
    );
  });

  it('renames a taxonomy label from the manage modal', async () => {
    vi.mocked(api.renameNewDedupTaxonomyLabel).mockResolvedValue({
      data: { ...OVERVIEW.labels[0], label: 'interier - kuchyn nova' },
    });
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    fireEvent.click(screen.getByText('Modify labels'));
    fireEvent.click(await screen.findByText('rename'));
    // Scope to the modal: the proposal tiles carry their own tag pickers,
    // which are seeded with this same label text.
    const modal = within(screen.getByRole('dialog'));
    fireEvent.change(modal.getByDisplayValue('interier - kuchyne'), {
      target: { value: 'interier - kuchyn nova' },
    });
    fireEvent.click(modal.getByText('Save'));
    await waitFor(() =>
      expect(api.renameNewDedupTaxonomyLabel).toHaveBeenCalledWith(1, 'interier - kuchyn nova'),
    );
  });

  it('removes a taxonomy label from the manage modal after the two-step confirm', async () => {
    vi.mocked(api.removeNewDedupTaxonomyLabel).mockResolvedValue({
      data: { label: 'interier - kuchyne', deleted_training_examples: 12, deleted_proposals: 4 },
    });
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    fireEvent.click(screen.getByText('Modify labels'));
    fireEvent.click(await screen.findByLabelText('Remove interier - kuchyne'));
    expect(api.removeNewDedupTaxonomyLabel).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('Remove'));
    await waitFor(() => expect(api.removeNewDedupTaxonomyLabel).toHaveBeenCalledWith(1));
  });

  it('renaming an unrelated label in the manage modal never hijacks the active proposals filter', async () => {
    const TWO_LABELS = {
      sample_size: 42,
      labels: [
        ...OVERVIEW.labels,
        {
          id: 2, label: 'koupelna', family: null, active: true,
          created_at: '2026-08-01T00:00:00Z', confirmed_count: 0, pending_count: 0,
          dismissed_count: 0,
        },
      ],
    };
    vi.mocked(api.getNewDedupLabelingOverview).mockResolvedValue({ data: TWO_LABELS });
    vi.mocked(api.renameNewDedupTaxonomyLabel).mockResolvedValue({
      data: { ...TWO_LABELS.labels[1], label: 'koupelna-v2' },
    });
    renderPage();
    // Filter proposals to "interier - kuchyne" via the bar chart first.
    fireEvent.click(await screen.findByRole('button', { name: 'interier - kuchyne' }));
    await waitFor(() =>
      expect(api.listNewDedupProposals).toHaveBeenCalledWith(
        expect.objectContaining({ label: 'interier - kuchyne' }),
      ),
    );

    // Open the manage modal and rename the UNRELATED "koupelna" row.
    fireEvent.click(screen.getByText('Modify labels'));
    const koupelnaRow = (await screen.findByLabelText('Remove koupelna')).closest('div')!
      .parentElement!.parentElement!;
    fireEvent.click(within(koupelnaRow).getByText('rename'));
    fireEvent.change(within(koupelnaRow).getByDisplayValue('koupelna'), {
      target: { value: 'koupelna-v2' },
    });
    fireEvent.click(within(koupelnaRow).getByText('Save'));
    await waitFor(() =>
      expect(api.renameNewDedupTaxonomyLabel).toHaveBeenCalledWith(2, 'koupelna-v2'),
    );

    // The filter must still read "interier - kuchyne" — no call to
    // listNewDedupProposals with label 'koupelna-v2' should ever happen.
    expect((screen.getByLabelText('Tag') as HTMLSelectElement).value).toBe(
      'interier - kuchyne',
    );
    expect(api.listNewDedupProposals).not.toHaveBeenCalledWith(
      expect.objectContaining({ label: 'koupelna-v2' }),
    );
  });

  it('removing the currently-filtered label from the manage modal clears the filter', async () => {
    vi.mocked(api.removeNewDedupTaxonomyLabel).mockResolvedValue({
      data: { label: 'interier - kuchyne', deleted_training_examples: 12, deleted_proposals: 4 },
    });
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'interier - kuchyne' }));
    await waitFor(() =>
      expect((screen.getByLabelText('Tag') as HTMLSelectElement).value).toBe(
        'interier - kuchyne',
      ),
    );

    fireEvent.click(screen.getByText('Modify labels'));
    fireEvent.click(await screen.findByLabelText('Remove interier - kuchyne'));
    fireEvent.click(screen.getByText('Remove'));
    await waitFor(() => expect(api.removeNewDedupTaxonomyLabel).toHaveBeenCalledWith(1));
    expect((screen.getByLabelText('Tag') as HTMLSelectElement).value).toBe('');
  });

  it('grows the sample with the entered count', async () => {
    vi.mocked(api.growNewDedupSample).mockResolvedValue({ data: { added: 50 } });
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    const countInput = screen.getByDisplayValue('200');
    fireEvent.change(countInput, { target: { value: '50' } });
    fireEvent.click(screen.getByText('Grow sample'));
    await waitFor(() =>
      expect(api.growNewDedupSample).toHaveBeenCalledWith(50, null),
    );
  });

  it('shows an inline pending state while growing the sample, so a slow request never reads as "nothing happened"', async () => {
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
    await waitFor(() =>
      expect(screen.getByText(/No new images matched/)).toBeInTheDocument(),
    );
  });

  it('shows the proposed label by default and switches to the original on toggle', async () => {
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    // Default view = "New tag": the proposal's label wins the badge.
    expect(await screen.findByText('interier - kuchyne', { selector: 'span' })).toBeInTheDocument();
    fireEvent.click(screen.getByText('Original tag'));
    // Original = image.clip_fine_tag ('kitchen') via imageTagLabel's Czech map.
    await waitFor(() => expect(screen.getByText('kuchyně')).toBeInTheDocument());
  });

  it('confirms a single proposal and drops that tile from Pending WITHOUT refetching the grid', async () => {
    vi.mocked(api.confirmNewDedupProposal).mockResolvedValue({ data: CONFIRM_RESULT });
    renderPage();
    await screen.findByText('Confirm');
    const callsBefore = vi.mocked(api.listNewDedupProposals).mock.calls.length;
    fireEvent.click(screen.getByText('Confirm'));
    await waitFor(() =>
      // An untouched Confirm sends NO label — the server then uses the
      // proposal's own stored label, so a rename landing between page load
      // and click can't be undone by echoing back a stale spelling.
      expect(api.confirmNewDedupProposal).toHaveBeenCalledWith(
        101, 'openai/clip-vit-large-patch14', undefined,
      ),
    );
    // The reviewed tile leaves this tab — but by a local cache patch, not a
    // refetch. Refetching re-renders (and, on tied proposed_at values,
    // re-orders) every remaining tile, which is the churn this page avoids.
    await waitFor(() => expect(screen.getByText('No pending proposals.')).toBeInTheDocument());
    expect(vi.mocked(api.listNewDedupProposals).mock.calls.length).toBe(callsBefore);
  });

  it('leaves every other tile untouched when one is confirmed on Pending', async () => {
    vi.mocked(api.confirmNewDedupProposal).mockResolvedValue({ data: CONFIRM_RESULT });
    vi.mocked(api.listNewDedupProposals).mockResolvedValue({
      data: [
        PROPOSALS[0],
        { ...PROPOSALS[0], image_id: 102, label: 'exterier - fasada' },
        { ...PROPOSALS[0], image_id: 103, label: 'garaz' },
      ],
    });
    vi.mocked(queries.fetchImagesByImageIds).mockResolvedValue(
      new Map([
        [101, IMAGE], [102, { ...IMAGE, id: 102 }], [103, { ...IMAGE, id: 103 }],
      ]),
    );
    renderPage();
    await waitFor(() => expect(screen.getAllByPlaceholderText('tag…')).toHaveLength(3));
    const imageCallsBefore = vi.mocked(queries.fetchImagesByImageIds).mock.calls.length;

    fireEvent.click(screen.getAllByText('Confirm')[0]);

    await waitFor(() => expect(screen.getAllByPlaceholderText('tag…')).toHaveLength(2));
    // Order preserved, and no image re-fetch: the photo cache accumulates by
    // id instead of being keyed on the current (now changed) id list.
    expect(
      screen.getAllByPlaceholderText('tag…').map((i) => (i as HTMLInputElement).value),
    ).toEqual(['exterier - fasada', 'garaz']);
    expect(vi.mocked(queries.fetchImagesByImageIds).mock.calls.length).toBe(imageCallsBefore);
  });

  it('confirms with a corrected tag when the suggestion is wrong', async () => {
    vi.mocked(api.confirmNewDedupProposal).mockResolvedValue({
      data: { ...CONFIRM_RESULT, label: 'interier - loznice', corrected: true },
    });
    renderPage();
    await screen.findByText('Confirm');
    // The tile's picker is seeded with the suggestion; typing a different
    // taxonomy label and confirming must send THAT, not the suggestion.
    const picker = screen.getByPlaceholderText('tag…');
    fireEvent.change(picker, { target: { value: 'interier - loznice' } });
    fireEvent.blur(picker);
    fireEvent.click(screen.getByText('Confirm'));
    await waitFor(() =>
      expect(api.confirmNewDedupProposal).toHaveBeenCalledWith(
        101, 'openai/clip-vit-large-patch14', 'interier - loznice',
      ),
    );
  });

  it('keeps the tag dropdown un-clipped — no overflow-hidden ancestor inside the card', async () => {
    renderPage();
    const picker = await screen.findByPlaceholderText('tag…');
    fireEvent.focus(picker);
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

  it('renders the tag picker after the action row so its dropdown cannot cover Confirm', async () => {
    renderPage();
    const picker = await screen.findByPlaceholderText('tag…');
    const confirm = screen.getByText('Confirm');
    // The dropdown opens downward out of the picker. If the picker preceded
    // the buttons in document order it would paint over them and eat the
    // first click aimed at Confirm (LabelCombobox keeps focus on mousedown,
    // so the option, not the button, receives it).
    const order = picker.compareDocumentPosition(confirm);
    expect(order & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy();
  });

  it('keeps per-model drafts separate for the same image', async () => {
    // One image, two models' proposals — correcting one must not rewrite the
    // other (label_proposals' PK is (image_id, model), so both are real rows).
    vi.mocked(api.listNewDedupProposals).mockResolvedValue({
      data: [
        PROPOSALS[0],
        { ...PROPOSALS[0], model: 'older-model', label: 'exterier - fasada' },
      ],
    });
    renderPage();
    const pickers = await screen.findAllByPlaceholderText('tag…');
    expect(pickers).toHaveLength(2);

    fireEvent.change(pickers[0], { target: { value: 'interier - loznice' } });
    fireEvent.blur(pickers[0]);

    await waitFor(() =>
      expect((pickers[0] as HTMLInputElement).value).toBe('interier - loznice'),
    );
    // The second tile still shows its own model's suggestion.
    expect((pickers[1] as HTMLInputElement).value).toBe('exterier - fasada');
  });

  it('takes a corrected tile out of the batch so bulk-confirm cannot discard the fix', async () => {
    // Two pending proposals; select both, then correct the first one's tag.
    // The batch endpoint writes each proposal's OWN label, so a corrected
    // tile must drop out of the selection rather than be silently confirmed
    // under the model's label.
    vi.mocked(api.listNewDedupProposals).mockResolvedValue({
      data: [PROPOSALS[0], { ...PROPOSALS[0], image_id: 102, label: 'exterier - fasada' }],
    });
    vi.mocked(api.bulkConfirmNewDedupProposals).mockResolvedValue({
      data: { confirmed: 1, model: 'openai/clip-vit-large-patch14', image_ids: [102] },
    });
    renderPage();
    fireEvent.click(await screen.findByText('Select all'));
    expect(screen.getByText('2 selected')).toBeInTheDocument();

    const picker = screen.getAllByPlaceholderText('tag…')[0];
    fireEvent.change(picker, { target: { value: 'interier - loznice' } });
    fireEvent.blur(picker);

    await waitFor(() => expect(screen.getByText('1 selected')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Confirm selected'));
    await waitFor(() =>
      expect(api.bulkConfirmNewDedupProposals).toHaveBeenCalledWith(
        'openai/clip-vit-large-patch14',
        [102],
      ),
    );
  });

  it('relabels an already-confirmed image in place via the training-example endpoint', async () => {
    vi.mocked(api.listNewDedupProposals).mockResolvedValue({
      data: [{ ...PROPOSALS[0], status: 'confirmed', reviewed_by: 'operator' }],
    });
    vi.mocked(api.setTrainingExample).mockResolvedValue({
      data: { image_id: 101, label: 'interier - loznice', updated_at: 't' },
    });
    renderPage();
    fireEvent.click(await screen.findByRole('tab', { name: 'Confirmed' }));
    const picker = await screen.findByPlaceholderText('tag…');
    fireEvent.change(picker, { target: { value: 'interier - loznice' } });
    fireEvent.blur(picker);
    fireEvent.click(await screen.findByText('Save tag'));
    await waitFor(() =>
      expect(api.setTrainingExample).toHaveBeenCalledWith({
        image_id: 101,
        label: 'interier - loznice',
      }),
    );
    // Confirming a proposal is a different write path — relabelling an image
    // already in the training set must not go back through it.
    expect(api.confirmNewDedupProposal).not.toHaveBeenCalled();
  });

  it('offers no tag picker on a dismissed proposal (it is not in the training set)', async () => {
    vi.mocked(api.listNewDedupProposals).mockResolvedValue({
      data: [{ ...PROPOSALS[0], status: 'dismissed', reviewed_by: 'operator' }],
    });
    renderPage();
    fireEvent.click(await screen.findByRole('tab', { name: 'Dismissed' }));
    await waitFor(() => expect(screen.getByText(/dismissed/)).toBeInTheDocument());
    expect(screen.queryByPlaceholderText('tag…')).not.toBeInTheDocument();
  });

  it('dismisses a single proposal and drops the tile without refetching the grid', async () => {
    vi.mocked(api.dismissNewDedupProposal).mockResolvedValue({ data: PROPOSALS[0] });
    renderPage();
    await screen.findByText('Dismiss');
    const callsBefore = vi.mocked(api.listNewDedupProposals).mock.calls.length;
    fireEvent.click(screen.getByText('Dismiss'));
    await waitFor(() =>
      expect(api.dismissNewDedupProposal).toHaveBeenCalledWith(
        101, 'openai/clip-vit-large-patch14',
      ),
    );
    await waitFor(() => expect(screen.getByText('No pending proposals.')).toBeInTheDocument());
    expect(vi.mocked(api.listNewDedupProposals).mock.calls.length).toBe(callsBefore);
  });

  it('batch-confirms selected proposals and the grid empties in place', async () => {
    vi.mocked(api.bulkConfirmNewDedupProposals).mockResolvedValue({
      data: { confirmed: 1, model: 'openai/clip-vit-large-patch14', image_ids: [101] },
    });
    renderPage();
    await screen.findByText('Select all');
    const callsBefore = vi.mocked(api.listNewDedupProposals).mock.calls.length;
    fireEvent.click(screen.getByText('Select all'));
    fireEvent.click(screen.getByText('Confirm selected'));
    await waitFor(() =>
      expect(api.bulkConfirmNewDedupProposals).toHaveBeenCalledWith(
        'openai/clip-vit-large-patch14', [101],
      ),
    );
    await waitFor(() => expect(screen.getByText('No pending proposals.')).toBeInTheDocument());
    expect(vi.mocked(api.listNewDedupProposals).mock.calls.length).toBe(callsBefore);
  });

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

  // --- chart collapse ------------------------------------------------------

  it('collapses the taxonomy chart, keeping its header (and the coverage filter) reachable', async () => {
    renderPage();
    const bar = await screen.findByRole('button', { name: 'interier - kuchyne' });
    expect(bar).toBeInTheDocument();

    const header = screen.getByRole('button', { expanded: true });
    fireEvent.click(header);

    expect(screen.queryByRole('button', { name: 'interier - kuchyne' })).not.toBeInTheDocument();
    // Folded, but not gone: the header and the tag-coverage ceiling it carries
    // stay usable, and the label count is still readable.
    expect(screen.getByRole('button', { expanded: false })).toBeInTheDocument();
    expect(screen.getByLabelText('Max training images per tag')).toBeInTheDocument();
    expect(screen.getByText(/Taxonomy v1 \(1 labels/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { expanded: false }));
    expect(await screen.findByRole('button', { name: 'interier - kuchyne' })).toBeInTheDocument();
  });

  // --- tag + coverage filters ---------------------------------------------

  it('filters the grid to one tag from the tag select', async () => {
    renderPage();
    await screen.findByRole('button', { name: 'interier - kuchyne' });
    fireEvent.change(screen.getByLabelText('Tag'), {
      target: { value: 'interier - kuchyne' },
    });
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
        labels: [
          { id: 1, label: 'interier - kuchyne', family: null, active: true,
            created_at: 't', confirmed_count: 12, pending_count: 0, dismissed_count: 0 },
          { id: 2, label: 'exterier - fasada', family: null, active: true,
            created_at: 't', confirmed_count: 200, pending_count: 0, dismissed_count: 0 },
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
    expect(screen.getByText(/1 of 2 tags/)).toBeInTheDocument();
  });

  it('never hides the tag the grid is currently filtered to, whatever the ceiling', async () => {
    vi.mocked(api.getNewDedupLabelingOverview).mockResolvedValue({
      data: {
        sample_size: 42,
        labels: [
          { id: 2, label: 'exterier - fasada', family: null, active: true,
            created_at: 't', confirmed_count: 200, pending_count: 0, dismissed_count: 0 },
        ],
      },
    });
    renderPage();
    // Wait for the taxonomy to land, else the select has no option to pick yet.
    await screen.findByRole('button', { name: 'exterier - fasada' });
    fireEvent.change(screen.getByLabelText('Tag'), {
      target: { value: 'exterier - fasada' },
    });
    fireEvent.change(screen.getByLabelText('Max training images per tag'), {
      target: { value: '10' },
    });
    // Otherwise the select would read "All tags" while the grid stayed filtered.
    expect((screen.getByLabelText('Tag') as HTMLSelectElement).value).toBe('exterier - fasada');
  });

  it('leaves the per-tile tag picker offering the WHOLE taxonomy, ceiling or not', async () => {
    vi.mocked(api.getNewDedupLabelingOverview).mockResolvedValue({
      data: {
        sample_size: 42,
        labels: [
          { id: 1, label: 'interier - kuchyne', family: null, active: true,
            created_at: 't', confirmed_count: 12, pending_count: 0, dismissed_count: 0 },
          { id: 2, label: 'exterier - fasada', family: null, active: true,
            created_at: 't', confirmed_count: 200, pending_count: 0, dismissed_count: 0 },
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

  // --- the All tab ---------------------------------------------------------

  const MIXED: NewDedupLabelProposal[] = [
    { ...PROPOSALS[0], image_id: 101, label: 'interier - kuchyne', status: 'pending',
      trained_label: null },
    { ...PROPOSALS[0], image_id: 102, label: 'exterier - fasada', status: 'confirmed',
      reviewed_by: 'operator', trained_label: 'exterier - fasada' },
    { ...PROPOSALS[0], image_id: 103, label: 'garaz', status: 'dismissed',
      reviewed_by: 'operator', trained_label: null },
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

    // One tile still awaits a decision; the tagged and the dismissed ones recede.
    await waitFor(() => expect(document.querySelectorAll('[data-dimmed]')).toHaveLength(2));
    // The bright one is the pending row — it's the only tile with Confirm/Dismiss.
    expect(screen.getAllByText('Confirm')).toHaveLength(1);
  });

  it('confirming on All greys the tile in place instead of moving or reordering anything', async () => {
    vi.mocked(api.listNewDedupProposals).mockResolvedValue({ data: MIXED });
    vi.mocked(api.confirmNewDedupProposal).mockResolvedValue({ data: CONFIRM_RESULT });
    vi.mocked(queries.fetchImagesByImageIds).mockResolvedValue(
      new Map([[101, IMAGE], [102, { ...IMAGE, id: 102 }], [103, { ...IMAGE, id: 103 }]]),
    );
    renderPage();
    fireEvent.click(await screen.findByRole('tab', { name: 'All' }));
    await waitFor(() => expect(screen.getAllByPlaceholderText('tag…')).toHaveLength(2));
    const callsBefore = vi.mocked(api.listNewDedupProposals).mock.calls.length;

    fireEvent.click(screen.getByText('Confirm'));

    // Same three tiles, same order, one more greyed and no refetch: this tab
    // is where the operator works continuously, so nothing may move.
    await waitFor(() => expect(document.querySelectorAll('[data-dimmed]')).toHaveLength(3));
    expect(screen.queryByText('Confirm')).not.toBeInTheDocument();
    expect(vi.mocked(api.listNewDedupProposals).mock.calls.length).toBe(callsBefore);
    expect(
      screen.getAllByPlaceholderText('tag…').map((i) => (i as HTMLInputElement).value),
    ).toEqual(['interier - kuchyne', 'exterier - fasada']);
  });

  it('offers the batch bar for pending rows on the All tab too', async () => {
    vi.mocked(api.listNewDedupProposals).mockResolvedValue({ data: MIXED });
    renderPage();
    fireEvent.click(await screen.findByRole('tab', { name: 'All' }));
    // Exactly the one pending row is selectable — a confirmed/dismissed row
    // can't be batch-reviewed again.
    fireEvent.click(await screen.findByText('Select all'));
    expect(screen.getByText('1 selected')).toBeInTheDocument();
  });
});
