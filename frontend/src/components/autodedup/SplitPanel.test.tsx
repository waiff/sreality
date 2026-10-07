/* The one split dialog (MS18): the server's preview in words, a refused letter
 * withholding the click, the acting account's items with their letter and
 * copies (folds read-only), each pick re-reading the preview so what the click
 * would skip is said, the click sending only what differs from the preview with
 * its `plan`, `stale` re-reading the page's adverts, one receipt toast — and
 * never a word about another account's items. lib/api is mocked at its two calls. */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useLocation } from 'react-router-dom';

import SplitPanel, { STALE_TEXT } from './SplitPanel';
import * as api from '@/lib/api';
import * as toast from '@/lib/toast';

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  getSplitPreview: vi.fn(),
  splitProperty: vi.fn(),
}));
vi.mock('@/lib/toast', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/toast')>()),
  pushToast: vi.fn(() => 1),
}));

const LETTERS = { 101: 'A', 202: 'B', 303: 'C', 404: 'C' };

function letter(
  l: string,
  ids: number[],
  lands: api.SplitPreviewLetter['lands'],
  property_id: number | null,
  over: Partial<api.SplitPreviewLetter> = {},
): api.SplitPreviewLetter {
  return { letter: l, listing_ids: ids, lands, property_id, joins: 0, refused: null, ...over };
}

const ITEMS: api.SplitPreviewItem[] = [
  { item: 'note:11', kind: 'note', label: 'Sousedi jsou hluční', letter: 'B', why: 'ad' },
  { item: 'pipeline', kind: 'pipeline', label: 'Prohlídka', letter: 'A', why: 'stays' },
  { item: 'collection:5', kind: 'collection', label: 'Brno 2+kk', letter: 'B', why: 'came_from', from_property_id: 43 },
  { item: 'fold:812', kind: 'tag', label: 'výhled', letter: 'B', why: 'fold', from_property_id: 43, skipped: null },
  { item: 'fold:813', kind: 'dismissal', label: null, letter: 'C', why: 'fold', from_property_id: 44, skipped: 'held' },
  { item: 'fold:814', kind: 'collection', label: 'Byty', letter: 'B', why: 'fold', from_property_id: 43, skipped: 'gone' },
];

function preview(over: Partial<api.SplitPreview> = {}): api.SplitPreview {
  return {
    property_id: 42,
    letters: [
      letter('A', [101], 'kept', 42),
      letter('B', [202], 'origin', 43),
      letter('C', [303, 404], 'new', null, { joins: 2 }),
    ],
    curation: ITEMS,
    rulings: {
      different: 5,
      taken_back: 1,
      inside: [
        { letter: 'A', pairs: [], sets: 1, taken_back: false },
        { letter: 'C', pairs: [[303, 404]], sets: 0, taken_back: true },
      ],
    },
    plan: 'plan-1',
    ...over,
  };
}

const RESULT: api.SplitResult = {
  property_id: 42,
  call_id: 'c',
  letters: [
    { letter: 'A', listing_ids: [101], property_id: 42, lands: 'kept', joined: null },
    { letter: 'B', listing_ids: [202], property_id: 43, lands: 'origin', joined: null },
    { letter: 'C', listing_ids: [303, 404], property_id: 90, lands: 'new', joined: 'g' },
  ],
  curation: [{ item: 'note:11', kind: 'note', label: 'Sousedi jsou hluční', letter: 'B', property_id: 43, copies: [] }],
  rulings: { different: 5, same: 1, taken_back: 1 },
};

const PORTAL: Record<number, string> = { 101: 'Sreality', 202: 'iDNES', 303: 'Bazoš', 404: 'Remax' };

function Where() {
  return <p data-testid="where">{useLocation().pathname}</p>;
}

function setup(props: { canonicalListingId?: number | null } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const onDone = vi.fn();
  const onCancel = vi.fn();
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SplitPanel
          propertyId={42}
          letters={LETTERS}
          canonicalListingId={props.canonicalListingId ?? 101}
          portalOf={(id) => PORTAL[id] ?? ''}
          priceOf={(id) => `${id} Kč`}
          onDone={onDone}
          onCancel={onCancel}
        />
        <Where />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { qc, onDone, onCancel };
}

const panel = () => screen.getByRole('group', { name: 'Rozdělení nemovitosti' });
const splitNow = () => fireEvent.click(within(panel()).getByRole('button', { name: 'Rozdělit nemovitost' }));
/* The click is offered on the preview of the picks as they are, once it is read. */
const pickedRead = () =>
  waitFor(() =>
    expect(within(panel()).getByRole('button', { name: 'Rozdělit nemovitost' })).toHaveAttribute(
      'aria-disabled',
      'false',
    ),
  );
const lines = async () => {
  await within(panel()).findByText(/zůstává v nemovitosti/);
  return within(panel())
    .getAllByRole('listitem')
    .map((li) => li.textContent);
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getSplitPreview).mockResolvedValue(preview());
  vi.mocked(api.splitProperty).mockResolvedValue(RESULT);
});

describe('<SplitPanel> the preview', () => {
  it('reads the server’s preview of every ad’s letter and says where each letter lands', async () => {
    setup();
    const said = await lines();
    expect(api.getSplitPreview).toHaveBeenCalledWith(42, '101:A,202:B,303:C,404:C');
    expect(said.slice(0, 3)).toEqual([
      'A — zůstává v nemovitosti #42: Sreality 101 Kč',
      'B — vrátí se do nemovitosti #43: iDNES 202 Kč',
      'C — odejde jako nová nemovitost (sloučí se z 2 nemovitostí do nové): Bazoš 303 Kč, Remax 404 Kč',
    ]);
    expect(said.slice(3, 6)).toEqual([
      'Mezi písmeny se zapíše „Různé“ u 5 dvojic s trvalým zákazem spojení.',
      'Sloučení písmene C zapíše „Stejné“ mezi hlavními inzeráty sloučených nemovitostí (jako každé sloučení) a vezme zpět 1 rozhodnutí „Různé“.',
      'V písmenu A platí „Různé“ (1); rozdělení ho nezmění.',
    ]);
  });

  it('with no letter joined, says nothing is written between ads of one letter', async () => {
    vi.mocked(api.getSplitPreview).mockResolvedValue(
      preview({
        letters: [letter('A', [101, 303, 404], 'kept', 42), letter('B', [202], 'origin', 43)],
        rulings: { different: 3, taken_back: 0, inside: [] },
      }),
    );
    setup();
    expect(await within(panel()).findByText(/Mezi písmeny/)).toHaveTextContent(
      'Mezi písmeny se zapíše „Různé“ u 3 dvojic s trvalým zákazem spojení; mezi inzeráty se stejným písmenem se nic nezapíše.',
    );
    expect(within(panel()).queryByText(/Stejné/)).toBeNull();
  });

  it('a letter rule 15 refuses is named and withholds the click', async () => {
    const sentence =
      'Inzerát typu Prodej a inzerát typu Pronájem systém nikdy nespojí do jedné nemovitosti, proto nemohou mít stejné písmeno. Dejte jim různá písmena.';
    vi.mocked(api.getSplitPreview).mockResolvedValue(
      preview({
        letters: [
          letter('A', [101], 'kept', 42),
          letter('B', [202], 'origin', 43),
          letter('C', [303, 404], 'new', null, {
            joins: 2,
            refused: { code: 'refused', message: sentence, ids: [303, 404] },
          }),
        ],
      }),
    );
    setup();
    expect(await within(panel()).findByText(sentence)).toBeInTheDocument();
    const button = within(panel()).getByRole('button', { name: 'Rozdělit nemovitost' });
    expect(button).toHaveAttribute('aria-disabled', 'true');
    splitNow();
    expect(api.splitProperty).not.toHaveBeenCalled();
  });

  it('says when the primary ad leaves with its letter', async () => {
    setup({ canonicalListingId: 202 });
    expect(
      await within(panel()).findByText(/Hlavní inzerát odejde s písmenem/),
    ).toHaveTextContent('Hlavní inzerát odejde s písmenem B: záhlaví nemovitosti #42 pak převezme inzerát písmene A.');
  });
});

describe('<SplitPanel> your items', () => {
  it('names each item, why it goes where it goes, and shows folds read-only', async () => {
    setup();
    await lines();
    const mine = screen.getByRole('group', { name: 'Vaše položky' });
    expect(mine).toHaveTextContent('poznámka „Sousedi jsou hluční“ — s inzerátem, u kterého vznikla');
    expect(mine).toHaveTextContent('zařazení v pipeline (Prohlídka) — zůstává');
    expect(mine).toHaveTextContent('kolekce „Brno 2+kk“ — přišla z nemovitosti #43');
    expect(mine).toHaveTextContent('štítek „výhled“ — obnoví se v B (při sloučení splynula)');
    expect(mine).toHaveTextContent('skrytí z vašeho Browse — neobnoví se — už ji tam máte');
    expect(mine).toHaveTextContent(
      'kolekce „Byty“ — neobnoví se — to, s čím při sloučení splynula, už na nemovitosti není',
    );
    // three choosable items, each with a letter and a copy per other letter; folds none
    expect(within(mine).getAllByRole('combobox')).toHaveLength(3);
    expect(within(mine).getAllByRole('checkbox')).toHaveLength(6);
    expect(within(mine).getByLabelText('Písmeno pro poznámka „Sousedi jsou hluční“')).toHaveValue('B');
  });

  it('sends only the choices that differ from the preview, the reason trimmed and the plan', async () => {
    setup();
    await lines();
    const mine = screen.getByRole('group', { name: 'Vaše položky' });
    fireEvent.change(within(mine).getByLabelText('Písmeno pro zařazení v pipeline (Prohlídka)'), {
      target: { value: 'B' },
    });
    const pipeline = within(mine).getByLabelText('Písmeno pro zařazení v pipeline (Prohlídka)').closest('li')!;
    fireEvent.click(within(pipeline).getByLabelText('+ kopie do C'));
    fireEvent.click(within(pipeline).getByLabelText('+ kopie do A'));
    // the note's letter picked and put back: no choice
    fireEvent.change(within(mine).getByLabelText('Písmeno pro poznámka „Sousedi jsou hluční“'), {
      target: { value: 'A' },
    });
    fireEvent.change(within(mine).getByLabelText('Písmeno pro poznámka „Sousedi jsou hluční“'), {
      target: { value: 'B' },
    });
    fireEvent.change(within(panel()).getByRole('textbox', { name: 'Důvod rozdělení (nepovinné)' }), {
      target: { value: '  jiné patro ' },
    });
    await pickedRead();
    splitNow();
    await waitFor(() =>
      expect(api.splitProperty).toHaveBeenCalledWith(42, {
        letters: LETTERS,
        choices: { pipeline: { to: 'B', copies: ['A', 'C'] } },
        reason: 'jiné patro',
        expect: 'plan-1',
      }),
    );
  });

  it('each pick re-reads the preview with it, and says what the click would then not make', async () => {
    vi.mocked(api.getSplitPreview).mockImplementation(async (_id, _letters, choices) =>
      choices
        ? preview({
            curation: ITEMS.map((item) =>
              item.item === 'pipeline'
                ? { ...item, copies: [{ letter: 'C', skipped: null }] }
                : item.item === 'fold:813'
                  ? { ...item, skipped: 'card' }
                  : item,
            ),
          })
        : preview(),
    );
    setup();
    await lines();
    const mine = screen.getByRole('group', { name: 'Vaše položky' });
    const pipeline = within(mine).getByLabelText('Písmeno pro zařazení v pipeline (Prohlídka)').closest('li')!;
    fireEvent.click(within(pipeline).getByLabelText('+ kopie do C'));
    await waitFor(() =>
      expect(api.getSplitPreview).toHaveBeenLastCalledWith(
        42,
        '101:A,202:B,303:C,404:C',
        '{"pipeline":{"to":"A","copies":["C"]}}',
      ),
    );
    expect(
      await within(mine).findByText(/neobnoví se — máte tam otevřený obchod v pipeline/),
    ).toBeInTheDocument();
    // unticked again: no pick left, the preview as it was
    fireEvent.click(within(pipeline).getByLabelText('+ kopie do C'));
    await waitFor(() =>
      expect(vi.mocked(api.getSplitPreview).mock.calls.at(-1)).toEqual([42, '101:A,202:B,303:C,404:C']),
    );
  });

  it('a copy the click would not make says why beside its box', async () => {
    vi.mocked(api.getSplitPreview).mockImplementation(async (_id, _letters, choices) =>
      preview({
        curation: choices
          ? ITEMS.map((item) =>
              item.item === 'collection:5' ? { ...item, copies: [{ letter: 'A', skipped: 'held' }] } : item,
            )
          : ITEMS,
      }),
    );
    setup();
    await lines();
    const row = screen.getByLabelText('Písmeno pro kolekce „Brno 2+kk“').closest('li')!;
    fireEvent.click(within(row).getByLabelText('+ kopie do A'));
    expect(await within(row).findByText(/nevytvoří se — už ji tam máte/)).toBeInTheDocument();
  });

  it('a copy never goes to the letter that gets the item', async () => {
    setup();
    await lines();
    const select = screen.getByLabelText('Písmeno pro kolekce „Brno 2+kk“');
    const row = select.closest('li')!;
    fireEvent.click(within(row).getByLabelText('+ kopie do C'));
    fireEvent.change(select, { target: { value: 'C' } });
    expect(within(row).queryByLabelText('+ kopie do C')).toBeNull();
    await pickedRead();
    splitNow();
    await waitFor(() =>
      expect(vi.mocked(api.splitProperty).mock.calls[0][1].choices).toEqual({
        'collection:5': { to: 'C', copies: [] },
      }),
    );
  });

  it('never speaks of another account', async () => {
    setup();
    await lines();
    expect(panel().textContent).not.toMatch(/účt|account|jiný uživatel/i);
  });
});

describe('<SplitPanel> the click', () => {
  it('done: one receipt toast, the re-read, and the letters handed back', async () => {
    const { qc, onDone } = setup();
    const invalidate = vi.spyOn(qc, 'invalidateQueries');
    await lines();
    splitNow();
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));
    expect(toast.pushToast).toHaveBeenCalledTimes(1);
    const [kind, text, ttl, action] = vi.mocked(toast.pushToast).mock.calls[0];
    expect([kind, ttl, action?.label]).toEqual(['ok', 0, 'Otevřít #43']);
    expect(text).toBe(
      'Rozděleno: A zůstává v #42; B → #43; C → nová #90. Do B: poznámka „Sousedi jsou hluční“. ' +
        '„Různé“ zapsáno u 5 dvojic. „Stejné“ zapsáno u 1 dvojice (sloučení písmene C). ' +
        'Zrušená rozhodnutí „Různé“: 1.',
    );
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['property'] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['merged-adverts'] });
    act(() => action!.onClick());
    expect(screen.getByTestId('where')).toHaveTextContent('/property/43');
  });

  it('stale: says so, re-reads, and nothing looks done', async () => {
    vi.mocked(api.splitProperty).mockRejectedValue(
      new api.ApiError('x', 409, {
        detail: { code: 'stale', message: 'Nemovitost se mezitím změnila; nic se nezapsalo.', ids: [] },
      }),
    );
    const { qc, onDone } = setup();
    const invalidate = vi.spyOn(qc, 'invalidateQueries');
    await lines();
    splitNow();
    expect(await within(panel()).findByRole('alert')).toHaveTextContent(STALE_TEXT);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['merged-adverts'] });
    expect(onDone).not.toHaveBeenCalled();
    expect(toast.pushToast).not.toHaveBeenCalled();
  });

  it('any other refusal is said as the server said it', async () => {
    vi.mocked(api.splitProperty).mockRejectedValue(
      new api.ApiError('x', 409, {
        detail: { code: 'busy', message: 'Nemovitost se právě mění, zkuste to za chvíli.', ids: [42] },
      }),
    );
    setup();
    await lines();
    splitNow();
    expect(await within(panel()).findByRole('alert')).toHaveTextContent(
      'Chyba: Nemovitost se právě mění, zkuste to za chvíli.',
    );
  });

  it('in flight: the button says so and keeps focus, and a second click sends nothing', async () => {
    let finish: (r: api.SplitResult) => void = () => {};
    vi.mocked(api.splitProperty).mockReturnValue(new Promise((resolve) => (finish = resolve)));
    const { onDone } = setup();
    await lines();
    splitNow();
    const busy = await within(panel()).findByRole('button', { name: 'Probíhá…' });
    expect(busy).toHaveAttribute('aria-busy', 'true');
    expect(busy).not.toBeDisabled();
    fireEvent.click(busy);
    expect(api.splitProperty).toHaveBeenCalledTimes(1);
    await act(async () => finish(RESULT));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
  });

  it('Zrušit hands the letters back without a call', async () => {
    const { onCancel } = setup();
    await lines();
    fireEvent.click(within(panel()).getByRole('button', { name: 'Zrušit' }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(api.splitProperty).not.toHaveBeenCalled();
  });

  it('a preview that cannot be read offers a retry', async () => {
    vi.mocked(api.getSplitPreview).mockRejectedValueOnce(new Error('HTTP 500'));
    setup();
    fireEvent.click(await within(panel()).findByRole('button', { name: 'Zkusit znovu' }));
    expect(await within(panel()).findByText(/zůstává v nemovitosti/)).toBeInTheDocument();
  });

  it('a preview refused as stale re-reads the page’s adverts, as it says, and offers it again', async () => {
    vi.mocked(api.getSplitPreview).mockRejectedValue(
      new api.ApiError('x', 409, {
        detail: { code: 'stale', message: 'Nemovitost se mezitím změnila; nic se nezapsalo.', ids: [505] },
      }),
    );
    const { qc } = setup();
    const invalidate = vi.spyOn(qc, 'invalidateQueries');
    expect(await within(panel()).findByRole('alert')).toHaveTextContent(STALE_TEXT);
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['property-sources'] }));
    invalidate.mockClear();
    fireEvent.click(within(panel()).getByRole('button', { name: 'Načíst znovu' }));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['property-sources'] });
    expect(api.splitProperty).not.toHaveBeenCalled();
  });
});
