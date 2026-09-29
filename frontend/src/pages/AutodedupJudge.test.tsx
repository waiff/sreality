/* The Judge page ("Soudce"): every pair the LLM judge read, beside the
 * operator's word. Pins:
 *   * blind by DEFAULT — an unruled row shows no judge word, no judge evidence,
 *     and no reason but the random sample's; a ruled row shows all of it;
 *     answering Stejné or Různé opens a row in place (the verdict overlay),
 *     "Nevím" opens nothing;
 *   * the engine's view in words on every row, and no "why it was not merged"
 *     on a pair the engine holds together;
 *   * a note saved on a ruled row re-posts its stored codes;
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
    verdict: null,
    judgement: {
      tier: 'vision',
      verdict: 'different_property',
      confidence: 0.93,
      model: 'gpt-5-mini',
      key_evidence: ['jiné patro'],
      contradicting_evidence: ['stejná adresa'],
    },
    reasons: ['engine'],
    /* A band pair joined into one group through a third advert: the server
     * sends no "why it was not merged" for it. */
    engine_view: 'together',
    obec_name: 'Jablonec nad Nisou',
    cast_obce_name: null,
    zone: 'band',
    score: 0.61,
    guard_veto: null,
    certificate: null,
    why_not_merged: null,
    ...over,
  };
}

const RULED = pair({
  listing_lo: 21,
  listing_hi: 22,
  verdict: {
    id: 5,
    kind: 'pair',
    listing_lo: 21,
    listing_hi: 22,
    verdict: 'same',
    note: 'stejná kuchyň',
    reasons: ['identical_photos'],
    decided_by: 'operator@example.com',
    decided_at: AT,
  },
  judgement: { tier: 'text', verdict: 'same_property', confidence: 0.8 },
  reasons: ['sample', 'operator'],
  engine_view: 'apart',
  why_not_merged: 'skóre v pásmu kontroly',
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
    /* Any reason but the sample's, even unnamed, would tell which way the judge
     * leaned. */
    expect(within(unruled).queryByText('Navrženo k posouzení')).toBeNull();
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

  it('opens nothing on "Nevím": it is not a peek before the real answer', async () => {
    const user = userEvent.setup();
    vi.mocked(api.postAutodedupVerdict).mockResolvedValue({
      store_ready: true,
      data: {
        id: 10, kind: 'pair', listing_lo: 11, listing_hi: 12, verdict: 'unsure', note: null,
        decided_by: 'operator@example.com', decided_at: AT,
      },
      must_not_link: false,
    });
    setup();
    const row = await rowOf('#11');
    await user.click(within(row).getByRole('button', { name: 'Nevím' }));
    await waitFor(() => expect(api.postAutodedupVerdict).toHaveBeenCalled());
    expect(within(row).queryByText(/soudce: jiná nemovitost/)).toBeNull();
    expect(within(row).getByText('soudce skryt')).toBeInTheDocument();
  });

  it('keeps a row blind on a stored "Nevím" or withdrawal', async () => {
    const nevim = { ...RULED.verdict!, listing_lo: 11, listing_hi: 12, verdict: 'unsure' as const };
    vi.mocked(api.getAutodedupJudgements).mockResolvedValue(page([pair({ verdict: nevim })]));
    setup();
    const row = await rowOf('#11');
    expect(within(row).queryByText(/soudce: jiná nemovitost/)).toBeNull();
    expect(within(row).getByText('soudce skryt')).toBeInTheDocument();
  });

  it('offers no control that reads the judge while blind', async () => {
    setup();
    await rowOf('#11');
    expect(screen.queryByLabelText('Soudce řekl')).toBeNull();
    expect(screen.queryByLabelText('Kdo četl')).toBeNull();
    expect(screen.queryByRole('group', { name: 'Soudce × engine' })).toBeNull();
    const choices = within(screen.getByLabelText('Výběr')).getAllByRole('option');
    expect(choices.map((o) => o.getAttribute('value'))).toEqual(['suggested', 'sample', 'all']);
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
    /* Blank keys never reach the wire (`blankToNull`). */
    expect(lastQuery()).toMatchObject({ reason: '', judge: '', tier: '', engine: '' });
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
    expect(lastQuery()?.reason).toBe('');
    expect(screen.getByLabelText('Výběr')).toHaveValue('suggested');
    expect(screen.getByText(/Navržené dvojice jsou seřazené/)).toBeInTheDocument();
    expect(screen.getByText('2 z 2 dvojic')).toBeInTheDocument();
    expect(screen.getByTestId('validation-sample')).toHaveTextContent('37 / 100');
  });

  it("says the engine's view in words, and never why a merged pair was not merged", async () => {
    setup();
    const together = await rowOf('#11');
    expect(within(together).getByText('Engine: jedna skupina')).toBeInTheDocument();
    expect(within(together).queryByText(/Proč to engine nesloučil/)).toBeNull();
    const apart = await rowOf('#21');
    expect(within(apart).getByText('Engine: odděleně')).toBeInTheDocument();
    expect(within(apart).getByText(/Proč to engine nesloučil/)).toBeInTheDocument();
  });

  it('keeps the stored codes when a note is saved on a ruled row', async () => {
    const user = userEvent.setup();
    setup();
    const row = await rowOf('#21');
    const note = within(row).getByLabelText('Poznámka');
    await user.clear(note);
    await user.type(note, 'stejná okna');
    await user.click(within(row).getByRole('button', { name: 'Uložit poznámku' }));
    expect(api.postAutodedupVerdict).toHaveBeenCalledWith({
      kind: 'pair', listing_lo: 21, listing_hi: 22, verdict: 'same',
      reasons: ['identical_photos'], note: 'stejná okna',
    });
  });

  it('shows no engine code without its meaning, and no internal names', async () => {
    setup();
    await rowOf('#11');
    expect(screen.getByText(/pásmo kontroly/)).toBeInTheDocument();
    expect(screen.queryByText(/generace/)).toBeNull();
    expect(screen.getByText(/vzorek je vylosovaný předem/)).toBeInTheDocument();
    expect(screen.queryByText(/semínko/)).toBeNull();
    expect(screen.queryByLabelText('Seznam')).toBeNull();
  });

  it('says the judge has read nothing yet when everything is empty', async () => {
    vi.mocked(api.getAutodedupJudgements).mockResolvedValue(
      page([], { reason: 'all', total: 0 }),
    );
    setup();
    expect(await screen.findByText('Soudce zatím nepřečetl žádnou dvojici.')).toBeInTheDocument();
  });

  it('says no pair fits when a filter empties the list', async () => {
    vi.mocked(api.getAutodedupJudgements).mockResolvedValue(page([], { total: 0 }));
    setup('/autodedup/judge?ruled=0');
    expect(await screen.findByText('Těmto filtrům neodpovídá žádná dvojice.')).toBeInTheDocument();
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

  it('shows a blind row the sample chip at most', () => {
    expect(reasonChips(pair({ reasons: ['sample', 'unsure'] }), false)).toEqual(['Náhodný vzorek']);
    expect(reasonChips(pair({ reasons: ['unsure'] }), false)).toEqual([]);
    expect(reasonChips(pair({ reasons: [] }), false)).toEqual([]);
    expect(reasonChips(pair({ reasons: ['sample', 'unsure'] }), true)).toEqual([
      'Náhodný vzorek', 'Soudce si nebyl jistý',
    ]);
  });
});
