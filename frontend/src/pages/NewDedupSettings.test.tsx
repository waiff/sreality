/* NewDedupSettings — the Wave 1 settings registry page.
 *
 * Hermetic: mock listNewDedupSettings/updateNewDedupSetting/resetNewDedupSetting.
 * Pins: category grouping renders, explanations default to a hover-only InfoHint
 * and the page's Compact/Detailed toggle switches every explanation inline, the
 * boolean toggle flips immediately, a number field only calls the API after
 * Enter (not per keystroke), the "not yet calibrated" / "edited" badges gate on
 * decided/is_override, and reset calls the reset endpoint. Backend CRUD is
 * covered by tests/api/test_new_dedup_routes.py; this only checks the wiring.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import NewDedupSettings from './NewDedupSettings';
import type { NewDedupSetting } from '@/lib/api';
import * as api from '@/lib/api';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    listNewDedupSettings: vi.fn(),
    updateNewDedupSetting: vi.fn(),
    resetNewDedupSetting: vi.fn(),
  };
});

const SETTINGS: NewDedupSetting[] = [
  {
    key: 'l0_geo_radius_m',
    category: 'l0_candidates',
    value_type: 'numeric',
    value: 75,
    default: 75,
    is_override: false,
    decided: true,
    explanation: 'How close two listings need to be to become a candidate pair.',
    enum_choices: null,
    minimum: null,
    maximum: null,
  },
  {
    key: 'l1_exact_attrs_enabled',
    category: 'l1_exact_attrs',
    value_type: 'boolean',
    value: false,
    default: false,
    is_override: false,
    decided: false,
    explanation: 'Whether the exact-attributes level is active.',
    enum_choices: null,
    minimum: null,
    maximum: null,
  },
  {
    key: 'l2_phash_hamming_threshold',
    category: 'l2_phash',
    value_type: 'integer',
    value: 8,
    default: 11,
    is_override: true,
    decided: true,
    explanation: 'How different two pHashes can be and still count as a match.',
    enum_choices: null,
    minimum: 0,
    maximum: 64,
  },
  {
    key: 'l2_phash_family_semantics',
    category: 'l2_phash',
    value_type: 'text',
    value: 'waterfall',
    default: 'waterfall',
    is_override: false,
    decided: true,
    explanation: 'How pHash evidence across tag families combines into one verdict.',
    enum_choices: ['waterfall', 'first_shared_family'],
    minimum: null,
    maximum: null,
  },
  {
    key: 'l3_embeddings_encoder',
    category: 'l3_embeddings',
    value_type: 'text',
    value: 'dinov3',
    default: 'dinov3',
    is_override: false,
    decided: true,
    explanation: 'Which image encoder produces the embeddings L3 compares.',
    enum_choices: null,
    minimum: null,
    maximum: null,
  },
];

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <NewDedupSettings />
    </QueryClientProvider>,
  );
}

describe('<NewDedupSettings>', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // The Compact/Detailed toggle and each category's open state persist to
    // localStorage (keyed by page/section id) — clear it so one test's clicks
    // can't leak into the next.
    localStorage.clear();
    vi.mocked(api.listNewDedupSettings).mockResolvedValue({ data: SETTINGS });
  });

  it('groups settings by category, compact by default with explanations on hover', async () => {
    renderPage();
    expect(await screen.findByText('L0 · Candidate selection')).toBeInTheDocument();
    expect(screen.getByText('L2 · Perceptual hash')).toBeInTheDocument();
    // Compact (the default): the explanation lives on the InfoHint icon's
    // title/aria-label, not as its own visible text node.
    expect(
      screen.getByTitle('How close two listings need to be to become a candidate pair.'),
    ).toBeInTheDocument();
    expect(
      screen.queryByText('How close two listings need to be to become a candidate pair.'),
    ).not.toBeInTheDocument();
  });

  it('Detailed mode shows every explanation inline', async () => {
    renderPage();
    await screen.findByText('L0 · Candidate selection');
    fireEvent.click(screen.getByRole('button', { name: 'Detailed' }));
    expect(
      await screen.findByText('How close two listings need to be to become a candidate pair.'),
    ).toBeInTheDocument();
    expect(
      screen.getByText('Whether the exact-attributes level is active.'),
    ).toBeInTheDocument();
  });

  it('tags an uncalibrated setting and an overridden one distinctly', async () => {
    renderPage();
    await screen.findByText('l1_exact_attrs_enabled');
    // The intro paragraph also uses the phrase "not yet calibrated" once —
    // the badge is the second occurrence.
    expect(screen.getAllByText('not yet calibrated')).toHaveLength(2);
    expect(screen.getByText('edited')).toBeInTheDocument();
    // l0_geo_radius_m is decided and not overridden — neither badge for it.
    const radiusRow = screen.getByText('l0_geo_radius_m').closest('div');
    expect(radiusRow?.textContent).not.toContain('not yet calibrated');
  });

  it('flips the boolean toggle immediately, no explicit save step', async () => {
    vi.mocked(api.updateNewDedupSetting).mockResolvedValue({
      ...SETTINGS[1],
      value: true,
      is_override: true,
    });
    renderPage();
    await screen.findByText('l1_exact_attrs_enabled');
    const toggle = screen.getByRole('button', { name: 'l1_exact_attrs_enabled', pressed: false });
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(api.updateNewDedupSetting).toHaveBeenCalledWith('l1_exact_attrs_enabled', true),
    );
  });

  it('number field only saves on Enter, not per keystroke', async () => {
    vi.mocked(api.updateNewDedupSetting).mockResolvedValue({
      ...SETTINGS[0],
      value: 90,
      is_override: true,
    });
    renderPage();
    const input = await screen.findByDisplayValue('75');
    fireEvent.change(input, { target: { value: '90' } });
    expect(api.updateNewDedupSetting).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() =>
      expect(api.updateNewDedupSetting).toHaveBeenCalledWith('l0_geo_radius_m', 90),
    );
  });

  it('reset button calls the reset endpoint for an overridden setting', async () => {
    vi.mocked(api.resetNewDedupSetting).mockResolvedValue({
      ...SETTINGS[2],
      value: 11,
      is_override: false,
    });
    renderPage();
    const resetButton = await screen.findByText(/reset to default \(11\)/);
    fireEvent.click(resetButton);
    await waitFor(() =>
      expect(api.resetNewDedupSetting).toHaveBeenCalledWith('l2_phash_hamming_threshold'),
    );
  });

  it('every value type announces itself by its setting key', async () => {
    renderPage();
    await screen.findByText('l0_geo_radius_m');
    // The four arms of SettingControl — switch, select, number, text — all
    // carry the key a sighted operator reads in the row's header.
    expect(
      screen.getByRole('button', { name: 'l1_exact_attrs_enabled' }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('combobox', { name: 'l2_phash_family_semantics' }),
    ).toHaveAccessibleName('l2_phash_family_semantics');
    expect(screen.getByRole('spinbutton', { name: 'l0_geo_radius_m' })).toHaveValue(75);
    expect(screen.getByRole('spinbutton', { name: 'l2_phash_hamming_threshold' })).toHaveValue(8);
    expect(screen.getByRole('textbox', { name: 'l3_embeddings_encoder' })).toHaveValue('dinov3');
  });

  it('enum select changes fire the update immediately', async () => {
    vi.mocked(api.updateNewDedupSetting).mockResolvedValue({
      ...SETTINGS[3],
      value: 'first_shared_family',
      is_override: true,
    });
    renderPage();
    const select = await screen.findByDisplayValue('waterfall');
    fireEvent.change(select, { target: { value: 'first_shared_family' } });
    await waitFor(() =>
      expect(api.updateNewDedupSetting).toHaveBeenCalledWith(
        'l2_phash_family_semantics',
        'first_shared_family',
      ),
    );
  });
});
