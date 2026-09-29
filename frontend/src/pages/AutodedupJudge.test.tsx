/* The Judge page ("Soudce"): every pair the LLM judge read, beside the
 * operator's word. Pins:
 *   * blind by DEFAULT — an unruled row shows no judge word, no judge evidence,
 *     and only a neutral reason chip; a ruled row shows all of it; answering a
 *     row opens it in place, through the verdict overlay;
 *   * while blind, no control reads the judge ("Soudce řekl", "Kdo četl",
 *     "Soudce × engine" and two "Výběr" values), and a link naming them is
 *     sanitised; turning blind off offers them;
 *   * the evidence link carries blind=1;
 *   * the page asks for no reason by default and shows the one the server used;
 *   * the empty states speak Czech;
 *   * no interactive control is nested inside another. */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useLocation } from 'react-router-dom';

import AutodedupJudge, {
  JUDGE_FILTER_DEFAULTS,
  listName,
  reasonChips,
  sanitizeJudgeFilters,
} from './AutodedupJudge';
import * as api from '@/lib/api';
import { expectNoNestedInteractive } from '@/test/a11y';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  getAutodedupJudgements: vi.fn(),
  getAutodedupValidationProgress: vi.fn(),
  postAutodedupVerdict: vi.fn(),
}));
vi.mock('@/lib/queries', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/queries')>()),
  fetchListingsForListingIds: vi.fn(async () => new Map()),
  fetchImagesForListingIds: vi.fn(async () => new Map()),
}));

const AT = '2026-09-29T08:00:00+00:00';

function pair(over: Partial<api.JudgedPair> = {}): api.JudgedPair {
  return {
    listing_lo: 11,
    listing_hi: 12,
    stratum: 'g2:s3_mf_band',
    ruled: false,
    verdict: null,
    operator_source: null,
    judgement: {
      tier: 'vision',
      verdict: 'different_property',
      confidence: 0.93,
      model: 'gpt-5-mini',
      key_evidence: ['jiné patro'],
      contradicting_evidence: ['stejná adresa'],
    },
    tiers_split: false,
    reasons: ['engine'],
    primary_reason: 'engine',
    operator_agreement: 'none',
    engine_agreement: 'disagrees',
    engine_view: 'together',
    together_now: false,
    obec_name: 'Jablonec nad Nisou',
    cast_obce_name: null,
    zone: 'band',
    score: 0.61,
    guard_veto: null,
    certificate: null,
    why_not_merged: 'skóre v pásmu kontroly',
    ...over,
  };
}

const RULED = pair({
  listing_lo: 21,
  listing_hi: 22,
  ruled: true,
  verdict: {
    id: 5,
    kind: 'pair',
    listing_lo: 21,
    listing_hi: 22,
    verdict: 'same',
    note: null,
    decided_by: 'operator@example.com',
    decided_at: AT,
  },
  operator_source: 'pair',
  judgement: { tier: 'text', verdict: 'same_property', confidence: 0.8 },
  reasons: ['sample', 'operator'],
  primary_reason: 'sample',
  operator_agreement: 'disagrees',
});

function page(
  items: api.JudgedPair[],
  over: Partial<api.JudgementsPage> = {},
): api.AutodedupEnvelope<api.JudgementsPage> {
  return {
    store_ready: true,
    data: {
      generation: 'rt',
      reason: 'suggested',
      items,
      next_after: null,
      total: items.length,
      facets: {
        reason: { suggested: 2, sample: 1, operator: 1, engine: 1 },
        judge: { different: 1, same: 1 },
        tier: { vision: 1, text: 1 },
        stratum: { 'g2:s3_mf_band': 2 },
        ruled: { '0': 1, '1': 1 },
        operator: { disagrees: 1 },
        engine: { disagrees: 1 },
      },
      towns: [{ grain: 'o', code: 563510, name: 'Jablonec nad Nisou', n: 2 }],
      ...over,
    },
  };
}

function LocationProbe() {
  return <i data-testid="search">{useLocation().search}</i>;
}

function setup(url = '/autodedup/judge') {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[url]}>
        <AutodedupJudge />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const lastQuery = () => vi.mocked(api.getAutodedupJudgements).mock.lastCall?.[0];
const rowOf = async (text: RegExp | string) =>
  (await screen.findAllByText(text))[0].closest('li')!;

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getAutodedupJudgements).mockResolvedValue(page([pair(), RULED]));
  vi.mocked(api.getAutodedupValidationProgress).mockResolvedValue({
    store_ready: true,
    data: {
      generation: 'rt',
      surface: 'judge',
      seed: 'v1',
      sample_size: 100,
      grain: 'pair',
      sample: { n: 100, n_reviewed: 37, n_not_same: 5 },
      total: { n: 412, n_reviewed: 60, n_not_same: 9 },
    },
  });
  vi.mocked(api.postAutodedupVerdict).mockResolvedValue({
    store_ready: true,
    data: {
      id: 9,
      kind: 'pair',
      listing_lo: 11,
      listing_hi: 12,
      verdict: 'same',
      note: null,
      decided_by: 'operator@example.com',
      decided_at: AT,
    },
    must_not_link: false,
  });
});

describe('<AutodedupJudge> blind by default', () => {
  it('hides the judge on an unruled row and shows it on a ruled one', async () => {
    setup();
    const unruled = await rowOf('#11');
    expect(within(unruled).queryByText(/soudce: jiná nemovitost/)).toBeNull();
    expect(within(unruled).queryByText(/jiné patro/)).toBeNull();
    expect(within(unruled).getByText('soudce skryt')).toBeInTheDocument();
    /* The specific reason would tell which way the judge leaned. */
    expect(within(unruled).getByText('Navrženo k posouzení')).toBeInTheDocument();
    expect(within(unruled).queryByText('Soudce a engine se neshodují')).toBeNull();

    const ruled = await rowOf('#21');
    expect(within(ruled).getByText(/soudce: stejná nemovitost/)).toBeInTheDocument();
    expect(within(ruled).getByText('Náhodný vzorek')).toBeInTheDocument();
    expect(within(ruled).getByText('Soudce říká opak než vy')).toBeInTheDocument();
    expectNoNestedInteractive(unruled);
  });

  it('opens a row in place the moment the operator answers it', async () => {
    const user = userEvent.setup();
    setup();
    const row = await rowOf('#11');
    await user.click(within(row).getByRole('button', { name: 'Stejné' }));
    expect(api.postAutodedupVerdict).toHaveBeenCalledWith({
      kind: 'pair', listing_lo: 11, listing_hi: 12, verdict: 'same', reasons: [], note: null,
    });
    await waitFor(() =>
      expect(within(row).getByText(/soudce: jiná nemovitost/)).toBeInTheDocument(),
    );
    expect(within(row).getByText(/jiné patro/)).toBeInTheDocument();
    expect(within(row).getByText('Soudce a engine se neshodují')).toBeInTheDocument();
    /* The list never reorders or refetches under the answering hand. */
    expect(api.getAutodedupJudgements).toHaveBeenCalledTimes(1);
  });

  it('offers no control that reads the judge while blind', async () => {
    setup();
    await rowOf('#11');
    expect(screen.queryByLabelText('Soudce řekl')).toBeNull();
    expect(screen.queryByLabelText('Kdo četl')).toBeNull();
    expect(screen.queryByRole('group', { name: 'Soudce × engine' })).toBeNull();
    const choices = within(screen.getByLabelText('Výběr')).getAllByRole('option');
    expect(choices.map((o) => o.getAttribute('value'))).toEqual([
      'suggested', 'sample', 'operator', 'all',
    ]);
    /* "Soudce × vy" stays: only a ruled row can agree or disagree. */
    expect(screen.getByRole('group', { name: 'Soudce × vy' })).toBeInTheDocument();
  });

  it('offers them, and shows every judge artefact, once blind is off', async () => {
    const user = userEvent.setup();
    setup();
    await rowOf('#11');
    await user.click(screen.getByRole('checkbox'));
    expect(screen.getByTestId('search').textContent).toContain('blind=0');
    expect(screen.getByLabelText('Soudce řekl')).toBeInTheDocument();
    expect(screen.getByLabelText('Kdo četl')).toBeInTheDocument();
    expect(screen.getByRole('group', { name: 'Soudce × engine' })).toBeInTheDocument();
    const row = await rowOf('#11');
    expect(within(row).getByText(/soudce: jiná nemovitost/)).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText('Soudce řekl'), 'different');
    await waitFor(() => expect(lastQuery()?.judge).toBe('different'));
  });

  it('drops a judge-reading filter a shared link carries while blind', async () => {
    setup('/autodedup/judge?reason=engine&judge=same&tier=gold&engine=agrees');
    await rowOf('#11');
    expect(lastQuery()).toMatchObject({ reason: null, judge: null, tier: null, engine: null });
  });

  it('carries blind into the evidence link', async () => {
    setup();
    const row = await rowOf('#11');
    expect(within(row).getByRole('link', { name: 'Celý důkaz' })).toHaveAttribute(
      'href',
      '/autodedup/pair/11/12?generation=rt&blind=1',
    );
  });
});

describe('<AutodedupJudge> the selection and the page around it', () => {
  it('asks for no reason and shows the one the server used', async () => {
    setup();
    await rowOf('#11');
    expect(lastQuery()?.reason).toBeNull();
    expect(screen.getByLabelText('Výběr')).toHaveValue('suggested');
    expect(screen.getByText(/Navržené páry jsou seřazené/)).toBeInTheDocument();
    expect(screen.getByText('2 z 2 dvojic')).toBeInTheDocument();
    expect(screen.getByTestId('validation-sample')).toHaveTextContent('37 / 100');
  });

  it('names the list a pair came from in plain words', async () => {
    setup();
    const row = await rowOf('#11');
    expect(within(row).getByText('Seznam: g2 · s3 mf band')).toBeInTheDocument();
    expect(within(screen.getByLabelText('Seznam')).getByRole('option', {
      name: 'g2 · s3 mf band (2)',
    })).toBeInTheDocument();
  });

  it('says the judge has read nothing yet when everything is empty', async () => {
    vi.mocked(api.getAutodedupJudgements).mockResolvedValue(
      page([], { reason: 'all', total: 0 }),
    );
    setup();
    expect(await screen.findByText('Soudce zatím nepřečetl žádný pár.')).toBeInTheDocument();
  });

  it('says no pair fits when a filter empties the list', async () => {
    vi.mocked(api.getAutodedupJudgements).mockResolvedValue(page([], { total: 0 }));
    setup('/autodedup/judge?ruled=0');
    expect(await screen.findByText('Těmto filtrům neodpovídá žádný pár.')).toBeInTheDocument();
  });

  it('says the store is not there yet on an un-migrated database', async () => {
    vi.mocked(api.getAutodedupJudgements).mockResolvedValue({ store_ready: false, data: null });
    setup();
    expect(
      await screen.findByText(/Úložiště programu v této databázi zatím není/),
    ).toBeInTheDocument();
  });
});

describe('the Judge page helpers', () => {
  it('keeps judge-reading values only when the page is not blind', () => {
    const raw = { ...JUDGE_FILTER_DEFAULTS, reason: 'unsure', judge: 'same', tier: 'text',
      engine: 'disagrees', operator: 'agrees' };
    expect(sanitizeJudgeFilters(raw)).toMatchObject({
      reason: '', judge: '', tier: '', engine: '', operator: 'agrees',
    });
    expect(sanitizeJudgeFilters({ ...raw, blind: '0' })).toMatchObject({
      reason: 'unsure', judge: 'same', tier: 'text', engine: 'disagrees',
    });
    expect(sanitizeJudgeFilters({ ...raw, blind: '0', judge: 'maybe', town: 'x' }))
      .toMatchObject({ judge: '', town: '' });
  });

  it('reads a list name generically', () => {
    expect(listName('g2:s1_ladder_only_edge')).toBe('g2 · s1 ladder only edge');
    expect(listName('control')).toBe('control');
  });

  it('shows a blind row one neutral chip at most', () => {
    expect(reasonChips(pair({ reasons: ['sample', 'unsure'] }), false)).toEqual(['Náhodný vzorek']);
    expect(reasonChips(pair({ reasons: ['unsure'] }), false)).toEqual(['Navrženo k posouzení']);
    expect(reasonChips(pair({ reasons: [] }), false)).toEqual([]);
    expect(reasonChips(pair({ reasons: ['sample', 'unsure'] }), true)).toEqual([
      'Náhodný vzorek', 'Soudce si nebyl jistý',
    ]);
  });
});
