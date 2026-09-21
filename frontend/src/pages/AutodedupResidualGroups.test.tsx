/* AutodedupResidual · the CANDIDATE-GROUP view — the Groups card on the unmerged side.
 *
 * Hermetic: every API read and the one write are mocked.
 *
 * Pins:
 *   * "po skupinách" is the DEFAULT view and "po dvojicích" is one click away,
 *     in the URL, with the pair queue behind it unchanged;
 *   * the letters start APART — the engine did not merge these adverts, so a
 *     card that opened with everything on A would be putting the engine's answer
 *     in the operator's mouth, backwards;
 *   * an already-merged group is ONE locked unit: bracketed, named, one letter
 *     select for the whole group, and moving it moves every advert in it;
 *   * the two shortcuts set the letters and store NOTHING — the operator still
 *     presses save, because every crossing pair is a permanent must-not-link;
 *   * the save names every advert of the card exactly once and names NO
 *     relation at all (D39): the letters are the whole statement;
 *   * blind mode: no judge artefact reaches the card;
 *   * the stored ruling is read back off the members' pair verdicts, so a reload
 *     shows the partition that exists rather than a blank one;
 *   * no interactive control is nested inside another.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useLocation } from 'react-router-dom';

import AutodedupResidual from './AutodedupResidual';
import * as api from '@/lib/api';
import { expectNoNestedInteractive } from '@/test/a11y';
import type {
  AutodedupCandidate,
  AutodedupCandidateMember,
  AutodedupVerdictRow,
} from '@/lib/api';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    getAutodedupCandidates: vi.fn(),
    getAutodedupCandidate: vi.fn(),
    postAutodedupCandidateSplitVerdict: vi.fn(),
    getAutodedupResidual: vi.fn(),
    getAutodedupBlocks: vi.fn(),
    getAutodedupGenerations: vi.fn(),
    postAutodedupVerdict: vi.fn(),
    getAutodedupVerdictReasons: vi.fn(),
    getAutodedupValidationProgress: vi.fn(),
  };
});

/* CARD grain: a candidate card is reviewed when every pair inside it is. */
const PROGRESS = {
  store_ready: true,
  data: {
    generation: 'g4',
    surface: 'candidates' as const,
    seed: 'v1',
    sample_size: 100,
    grain: 'candidate' as const,
    sample: { n: 100, n_reviewed: 12, n_not_same: 4 },
    total: { n: 1840, n_reviewed: 61, n_not_same: 40 },
  },
};

const GENERATIONS = {
  store_ready: true,
  data: {
    items: [
      {
        generation: 'g4',
        n_clusters: 870,
        n_members: 2100,
        n_conflicted: 0,
        last_changed_at: '2026-09-17T09:00:00Z',
      },
    ],
    latest: 'g4',
  },
};

function member(
  listing_id: number,
  over: Partial<AutodedupCandidateMember> = {},
): AutodedupCandidateMember {
  return {
    listing_id,
    source: 'sreality',
    source_url: `https://www.sreality.cz/detail/${listing_id}`,
    category_main: 'byt',
    category_type: 'prodej',
    disposition: '2+kk',
    area_m2: 54,
    floor: 3,
    price_czk: 5_900_000,
    first_seen_at: '2026-01-04T00:00:00Z',
    last_seen_at: '2026-03-01T00:00:00Z',
    is_active: true,
    cover: { storage_path: null, sreality_url: `https://img.example.invalid/${listing_id}.jpg` },
    n_images: 8,
    unit_key: 'l' + listing_id,
    unit_lock: null,
    ...over,
  };
}

/* THE MEASURED SHAPE: one lone advert against a merged group of three. Four
 * adverts, two units, three residual pairs — one card, one question. */
const CARD: AutodedupCandidate = {
  candidate_key: '50-abcdef0123',
  generation: 'g4',
  size: 4,
  n_units: 2,
  score_min: 0.41,
  score_max: 0.55,
  zones: { band: 3 },
  families: 5,
  family_names: ['ATTR', 'TXT'],
  block_key: 500123,
  block_grain: 'o',
  locked_cluster_keys: [900],
  units: [
    { unit_key: 'l50', cluster_key: null, listing_ids: [50] },
    { unit_key: 'c900', cluster_key: 900, listing_ids: [201, 202, 203] },
  ],
  n_pairs: 3,
  n_pairs_reviewed: 0,
  n_pairs_not_same: 0,
  reviewed: false,
  sources: ['bazos', 'sreality'],
  members: [
    member(50),
    member(201, { unit_key: 'c900', unit_lock: 900, source: 'bazos' }),
    member(202, { unit_key: 'c900', unit_lock: 900 }),
    member(203, { unit_key: 'c900', unit_lock: 900 }),
  ],
  member_verdicts: [],
};

const page = (items: AutodedupCandidate[], over: Record<string, unknown> = {}) => ({
  store_ready: true,
  data: {
    items,
    has_more: false,
    next_after: null,
    generation: 'g4',
    total: items.length,
    ...over,
  },
});

const SPLIT_RESULT = {
  store_ready: true,
  data: {
    candidate_key: CARD.candidate_key,
    cluster_verdict: null,
    n_pairs_same: 0,
    n_pairs_negative: 3,
    must_not_link_written: 3,
    must_not_link_retracted: 0,
    n_pairs_locked: 3,
    reversed_pairs: [],
  },
};

function LocationProbe() {
  const loc = useLocation();
  return <i data-testid="search">{loc.search}</i>;
}

function renderPage(entry = '/autodedup/residual') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[entry]}>
        <AutodedupResidual />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const card = async () => (await screen.findByText(/už sloučeno/)).closest('li')!;

/* Render and hand back the one card — the grouped view is the page's default,
 * so most tests need no entry at all. */
const renderCard = async (entry?: string) => {
  renderPage(entry);
  return card();
};

describe('<AutodedupResidual> · po skupinách', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getAutodedupCandidates).mockResolvedValue(page([CARD]));
    vi.mocked(api.getAutodedupResidual).mockResolvedValue({
      store_ready: true,
      data: { items: [], has_more: false, next_after: null, generation: 'g4', total: 0 },
    });
    vi.mocked(api.getAutodedupBlocks).mockResolvedValue({
      store_ready: true,
      data: { items: [], generation: 'g4' },
    });
    vi.mocked(api.getAutodedupGenerations).mockResolvedValue(GENERATIONS);
    vi.mocked(api.getAutodedupVerdictReasons).mockResolvedValue([
      { code: 'floor_plan_differs', label: 'Jiný půdorys' },
    ]);
    vi.mocked(api.getAutodedupValidationProgress).mockResolvedValue(PROGRESS);
    vi.mocked(api.postAutodedupCandidateSplitVerdict).mockResolvedValue(SPLIT_RESULT);
  });

  /* ------------------------------------------------------------ the switch */

  it('opens on the grouped view and reads the candidate queue, not the pair one', async () => {
    renderPage();
    await card();
    expect(api.getAutodedupCandidates).toHaveBeenCalled();
    /* The view that is not on screen asks nothing: two queues over one cohort
     * would double every page load for a list nobody is looking at. */
    expect(api.getAutodedupResidual).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'po skupinách' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
  });

  it('switches to the pair view, puts it in the URL, and the pair queue takes over', async () => {
    const user = userEvent.setup();
    renderPage();
    await card();
    await user.click(screen.getByRole('button', { name: 'po dvojicích' }));
    await waitFor(() => expect(api.getAutodedupResidual).toHaveBeenCalled());
    expect(screen.getByTestId('search').textContent).toContain('view=pairs');
    expect(screen.getByRole('button', { name: 'po dvojicích' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
  });

  it('a link that names the pair view arrives in it', async () => {
    renderPage('/autodedup/residual?view=pairs');
    await waitFor(() => expect(api.getAutodedupResidual).toHaveBeenCalled());
    expect(api.getAutodedupCandidates).not.toHaveBeenCalled();
  });

  /* -------------------------------------------------------- the card itself */

  it('shows every advert of the card and names the merged group it holds', async () => {
    const row = await renderCard();
    for (const id of [50, 201, 202, 203]) {
      /* getAllBy: the id names the advert AND its unit control. */
      expect(within(row).getAllByText(new RegExp(`#${id}\\b`)).length).toBeGreaterThan(0);
    }
    expect(within(row).getByText(/už sloučeno/)).toBeTruthy();
    expect(within(row).getByText(/#900/)).toBeTruthy();
    /* The header says what the card is, at advert AND unit grain. */
    expect(within(row).getByText(/4 inzerátů · 2 jednotek/)).toBeTruthy();
  });

  it('starts the units APART, because the engine did not merge them', async () => {
    const row = await renderCard();
    /* One select per UNIT — not one per advert: the merged group moves together.
     * TWO selects on the whole card and no third: since D39 the letters are the
     * only question, so there is no relation control beside them. */
    const lone = within(row).getByLabelText('Jednotka #50') as HTMLSelectElement;
    const locked = within(row).getByLabelText('Jednotka celé skupiny') as HTMLSelectElement;
    expect(within(row).getAllByRole('combobox')).toHaveLength(2);
    expect(within(row).queryByLabelText(/Vztah/)).toBeNull();
    expect(lone.value).toBe('A');
    expect(locked.value).toBe('B');
    expect(within(row).getByText(/nesloučil/)).toBeTruthy();
  });

  it('the locked group has ONE select for all of its adverts', async () => {
    const user = userEvent.setup();
    const row = await renderCard();
    const locked = within(row).getByLabelText('Jednotka celé skupiny');
    await user.selectOptions(locked, 'A');
    /* Every advert of the group followed, so the assignment the save sends can
     * never separate them — the server refuses that, and the control refuses it
     * first. */
    await user.click(within(row).getByRole('button', { name: 'Uložit rozhodnutí' }));
    await waitFor(() => expect(api.postAutodedupCandidateSplitVerdict).toHaveBeenCalled());
    const body = vi.mocked(api.postAutodedupCandidateSplitVerdict).mock.calls[0][0];
    expect(body.units.filter((u) => [201, 202, 203].includes(u.listing_id))
      .map((u) => u.unit)).toEqual(['A', 'A', 'A']);
  });

  /* -------------------------------------------------------- the two shortcuts */

  it('"Vše je jedna jednotka" puts every advert on A and stores nothing by itself', async () => {
    const user = userEvent.setup();
    const row = await renderCard();
    await user.click(within(row).getByRole('button', { name: 'Vše je jedna jednotka' }));
    expect((within(row).getByLabelText('Jednotka #50') as HTMLSelectElement).value).toBe('A');
    expect(
      (within(row).getByLabelText('Jednotka celé skupiny') as HTMLSelectElement).value,
    ).toBe('A');
    /* The shortcut is not a save: every crossing pair is a permanent
     * must-not-link, and a one-click save over eight adverts is 28 of them. */
    expect(api.postAutodedupCandidateSplitVerdict).not.toHaveBeenCalled();
  });

  it('"Nic k sobě nepatří" puts every unit on its own letter', async () => {
    const user = userEvent.setup();
    const row = await renderCard();
    await user.click(within(row).getByRole('button', { name: 'Vše je jedna jednotka' }));
    await user.click(within(row).getByRole('button', { name: 'Nic k sobě nepatří' }));
    expect((within(row).getByLabelText('Jednotka #50') as HTMLSelectElement).value).toBe('A');
    expect(
      (within(row).getByLabelText('Jednotka celé skupiny') as HTMLSelectElement).value,
    ).toBe('B');
    expect(api.postAutodedupCandidateSplitVerdict).not.toHaveBeenCalled();
  });

  /* -------------------------------------------------------------- the save */

  it('the save names every advert of the card, and no relation at all', async () => {
    const user = userEvent.setup();
    const row = await renderCard();
    await user.click(within(row).getByRole('button', { name: 'Uložit rozhodnutí' }));
    await waitFor(() => expect(api.postAutodedupCandidateSplitVerdict).toHaveBeenCalled());
    const body = vi.mocked(api.postAutodedupCandidateSplitVerdict).mock.calls[0][0];
    expect(body.candidate_key).toBe(CARD.candidate_key);
    /* The pass the CARD came from, never the queue's. */
    expect(body.generation).toBe('g4');
    expect(body.units.map((u) => u.listing_id).sort((a, b) => a - b)).toEqual([50, 201, 202, 203]);
    /* Two letters are two properties and the body says nothing more (D39): the
     * server defaults the missing relation to `different`. */
    expect('relation' in body).toBe(false);
    expect('relations' in body).toBe(false);
    /* NO reason chips: a candidate split writes no cluster row to carry them,
     * and the server answers 400 rather than dropping them silently. */
    expect('reasons' in body).toBe(false);
  });

  it('saves with no note and no chips — an annotation is never required', async () => {
    const user = userEvent.setup();
    const row = await renderCard();
    await user.click(within(row).getByRole('button', { name: 'Uložit rozhodnutí' }));
    await waitFor(() => expect(api.postAutodedupCandidateSplitVerdict).toHaveBeenCalled());
    const body = vi.mocked(api.postAutodedupCandidateSplitVerdict).mock.calls[0][0];
    /* The note rides along as null rather than blocking the save: the operator
     * annotates sometimes, not every time. */
    expect(body.note).toBeNull();
  });

  it('a 409 keeps the letters on screen and arms the save', async () => {
    const user = userEvent.setup();
    vi.mocked(api.postAutodedupCandidateSplitVerdict).mockRejectedValueOnce(
      new api.ApiError('this split takes back your earlier ruling on 1 pair(s)', 409, null),
    );
    const row = await renderCard();
    await user.click(within(row).getByRole('button', { name: 'Uložit rozhodnutí' }));
    expect(await within(row).findByText(/takes back your earlier ruling/)).toBeTruthy();
    const again = within(row).getByRole('button', { name: 'Přepsat a uložit' });
    await user.click(again);
    await waitFor(() =>
      expect(vi.mocked(api.postAutodedupCandidateSplitVerdict).mock.calls).toHaveLength(2),
    );
    expect(vi.mocked(api.postAutodedupCandidateSplitVerdict).mock.calls[1][0].confirm_retract)
      .toBe(true);
  });

  /* ------------------------------------------------------------- blind mode */

  it('carries no judge artefact at all', async () => {
    const row = await renderCard();
    expect(row.textContent).not.toMatch(/soudce/i);
    expect(row.textContent).not.toMatch(/judge/i);
    /* The engine's OWN evidence is not the thing being validated, and stays —
     * the score range (cs-CZ decimals), the zones and the families. */
    expect(row.textContent).toMatch(/0,41 – 0,55/);
    expect(within(row).getByText(/band 3/)).toBeTruthy();
    expect(within(row).getByText('ATTR')).toBeTruthy();
  });

  /* ------------------------------------------------- the stored ruling, read back */

  it('hydrates the letters from the stored pair verdicts after a reload', async () => {
    const stored: AutodedupVerdictRow = {
      kind: 'pair',
      listing_lo: 50,
      listing_hi: 201,
      verdict: 'same',
      note: null,
      decided_by: 'operator@example.com',
      decided_at: '2026-09-17T10:00:00Z',
    };
    vi.mocked(api.getAutodedupCandidates).mockResolvedValue(
      page([{ ...CARD, member_verdicts: [stored] }]),
    );
    const row = await renderCard();
    /* 50 and the merged group were ruled one property, so they share a letter —
     * read back off the store, not remembered in page state a reload throws
     * away (E50). */
    expect((within(row).getByLabelText('Jednotka #50') as HTMLSelectElement).value).toBe(
      (within(row).getByLabelText('Jednotka celé skupiny') as HTMLSelectElement).value,
    );
    expect(within(row).getByText(/Uloženo dříve/)).toBeTruthy();
  });

  it('a stored ruling that splits a merged group is collapsed onto the lock', async () => {
    /* The operator split group #900 on the Groups page. This card holds that
     * group as ONE unit, cannot honour both, and does not own the group — so it
     * shows what it will actually send rather than a save the server refuses. */
    const rows: AutodedupVerdictRow[] = [
      {
        kind: 'pair', listing_lo: 201, listing_hi: 202, verdict: 'same_building_different_unit',
        note: null, decided_by: 'operator@example.com', decided_at: '2026-09-17T10:00:00Z',
      },
    ];
    vi.mocked(api.getAutodedupCandidates).mockResolvedValue(
      page([{ ...CARD, member_verdicts: rows }]),
    );
    const user = userEvent.setup();
    const row = await renderCard();
    await user.click(within(row).getByRole('button', { name: 'Uložit rozhodnutí' }));
    await waitFor(() => expect(api.postAutodedupCandidateSplitVerdict).toHaveBeenCalled());
    const body = vi.mocked(api.postAutodedupCandidateSplitVerdict).mock.calls[0][0];
    const letters = body.units
      .filter((u) => [201, 202, 203].includes(u.listing_id))
      .map((u) => u.unit);
    expect(new Set(letters).size).toBe(1);
  });

  /* ----------------------------------------------------------- the counters */

  it('counts CARDS, and says which sample it is counting', async () => {
    renderPage('/autodedup/residual?sort=random');
    await card();
    await waitFor(() =>
      expect(api.getAutodedupValidationProgress).toHaveBeenCalledWith(
        expect.objectContaining({ surface: 'candidates' }),
      ),
    );
    const strip = (await screen.findByText(/Zkontrolováno/)).closest('div')!;
    /* cs-CZ groups thousands with a non-breaking space. */
    expect(strip.textContent?.replace(/\u00a0/g, ' ')).toMatch(/61 \/ 1 840 karet/);
    expect(
      screen.getByTestId('validation-sample').textContent?.replace(/\u00a0/g, ' '),
    ).toMatch(/12 \/ 100/);
  });

  it('the grouped view sends no display floor and no portal pair', async () => {
    renderPage();
    await card();
    const query = vi.mocked(api.getAutodedupCandidates).mock.calls[0][0]!;
    expect('min_score' in query).toBe(false);
    expect('source_pair' in query).toBe(false);
    expect(query.sort).toBe('weakest');
    expect(screen.queryByText('Portál A')).toBeNull();
  });

  it('the state filter sends the card vocabulary, not the five verdicts', async () => {
    const user = userEvent.setup();
    renderPage();
    await card();
    await user.selectOptions(screen.getByLabelText('Stav'), 'reviewed');
    await waitFor(() =>
      expect(
        vi.mocked(api.getAutodedupCandidates).mock.calls.at(-1)![0]!.verdict,
      ).toBe('reviewed'),
    );
  });

  /* ------------------------------------------------------------------- a11y */

  it('nests no interactive control inside another', async () => {
    const row = await renderCard();
    expectNoNestedInteractive(row);
  });
});
