/* NEW DEDUP · Tagging bake-off — the comparison page.
 *
 * Hermetic: the four read routes are mocked. What is pinned here is the reading
 * of the CONTRACT, not the layout:
 *   - a null precision/recall/f1 is "nothing proposed", never a zero, and the
 *     graded n is drawn beside it either way;
 *   - a `status: 'failed'` metric row is a decided cell carrying its note;
 *   - outcome needs a head, because an outcome is one head's verdict;
 *   - View B reads the threshold off the metric row, since /buckets has none;
 *   - every selector is in the URL, so a link reproduces the screen.
 * Backend shape is covered by tests/api/test_new_dedup_bakeoff.py.
 */

import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import NewDedupTaggingBakeoff from './NewDedupTaggingBakeoff';
import * as api from '@/lib/api';
import type { BakeoffMetric } from '@/lib/api';

vi.mock('@/lib/api');
vi.mock('@/lib/imageUrl', () => ({ imageSrc: () => 'blob:photo' }));

const ARM_A = {
  id: 7, run_id: 3, arm: 'dinov3-b16@768/bf16', dim: 768, status: 'ok', note: null,
  model: 'facebook/dinov3-vitb16', revision: '5931719ecafe', library: 'transformers',
  pooling: 'cls', resolution: 768, preprocessing: 'resize-shortest-768-centercrop',
  dtype: 'bfloat16',
};
const ARM_B = {
  ...ARM_A, id: 8, arm: 'clip-l14@336/fp16', dim: 1024, model: 'openai/clip-vit-large-patch14',
  resolution: 336, dtype: 'float16',
};

const RUNS = [
  {
    id: 3, created_at: '2026-09-08T12:00:00+00:00', label: 'set-1 v1', note: null,
    status: 'ok', manifest_key: 'bakeoff/3/manifest.json', heads: [19, 22],
    min_train_positives: 100, arms: [ARM_A, ARM_B],
  },
  {
    id: 2, created_at: '2026-09-01T09:00:00+00:00', label: 'pilot', note: null,
    status: 'failed', manifest_key: null, heads: [19], min_train_positives: 100,
    arms: [ARM_A],
  },
];

const base = (over: Partial<BakeoffMetric>): BakeoffMetric => ({
  arm_id: 7, arm: ARM_A.arm, mode: 'pos_neg', tag_id: 19, tag_label: 'interier - kuchyně',
  n_pos: 231, n_neg: 1004, n_groups: 612,
  cv_precision: null, cv_recall: null, cv_f1: null, cv_graded_n: 0,
  cv_tp: 0, cv_fp: 0, cv_tn: 0, cv_fn: 0,
  exam_precision: null, exam_recall: null, exam_f1: null, exam_graded_n: 0,
  exam_abstained_n: 0, exam_tp: 0, exam_fp: 0, exam_tn: 0, exam_fn: 0,
  threshold: 0.5, dataset_hash: '9f2c', status: 'ok', note: null,
  trained_at: '2026-09-08T12:31:04+00:00',
  ...over,
});

const METRICS: BakeoffMetric[] = [
  /* The best cell on the head, and — on the exam split — a head that proposed
   * nothing: every rate null while 250 photos were abstained on. */
  base({
    arm_id: 7, arm: ARM_A.arm, tag_id: 19,
    cv_precision: 0.94, cv_recall: 0.89, cv_f1: 0.915, cv_graded_n: 1235,
    cv_tp: 206, cv_fp: 13, cv_tn: 991, cv_fn: 25,
    exam_graded_n: 0, exam_abstained_n: 250,
  }),
  base({
    arm_id: 8, arm: ARM_B.arm, tag_id: 19,
    cv_precision: 0.71, cv_recall: 0.92, cv_f1: 0.802, cv_graded_n: 1235,
    cv_tp: 213, cv_fp: 87, cv_tn: 917, cv_fn: 18,
    exam_precision: 0.5, exam_recall: 0.4, exam_f1: 0.444, exam_graded_n: 41,
    exam_abstained_n: 209, exam_tp: 4, exam_fp: 4, exam_tn: 27, exam_fn: 6,
  }),
  /* A decided cell, not a missing one. */
  base({
    arm_id: 7, arm: ARM_A.arm, tag_id: 22, tag_label: 'interier - koupelna',
    status: 'failed', note: 'too few positive listing-groups for a grouped split',
    threshold: null,
  }),
  base({
    arm_id: 8, arm: ARM_B.arm, tag_id: 22, tag_label: 'interier - koupelna',
    cv_precision: 0.6, cv_recall: 0.6, cv_f1: 0.6, cv_graded_n: 400,
    cv_tp: 30, cv_fp: 20, cv_tn: 330, cv_fn: 20,
  }),
];

const IMAGES = {
  images: [
    {
      image_id: 555, listing_id: 99213, storage_path: 'images/2026/555.jpg',
      scores: [
        {
          arm_id: 7, arm: ARM_A.arm, mode: 'pos_neg' as const, tag_id: 19,
          tag_label: 'interier - kuchyně', split: 'cv' as const, fold: 2,
          label: 1 as const, score: 0.9713, predicted: true, outcome: 'tp' as const,
        },
        {
          arm_id: 8, arm: ARM_B.arm, mode: 'pos_neg' as const, tag_id: 19,
          tag_label: 'interier - kuchyně', split: 'cv' as const, fold: 2,
          label: 1 as const, score: 0.2101, predicted: false, outcome: 'fn' as const,
        },
      ],
    },
    {
      image_id: 777, listing_id: null, storage_path: 'images/2026/777.jpg',
      scores: [
        {
          arm_id: 7, arm: ARM_A.arm, mode: 'pos_neg' as const, tag_id: 19,
          tag_label: 'interier - kuchyně', split: 'cv' as const, fold: 1,
          label: 0 as const, score: 0.0221, predicted: false, outcome: 'tn' as const,
        },
      ],
    },
  ],
  next_after_image_id: 777,
};

const BUCKETS = {
  arm_id: 7, mode: 'pos_neg' as const, tag_id: 19, split: 'cv' as const,
  abstained_count: 12,
  buckets: {
    tp: { count: 206, tiles: [{ image_id: 555, listing_id: 99213, storage_path: 'a.jpg', score: 0.97, label: 1 as const, predicted: true, fold: 2 }] },
    fp: { count: 13, tiles: [{ image_id: 556, listing_id: null, storage_path: 'b.jpg', score: 0.88, label: 0 as const, predicted: true, fold: 1 }] },
    fn: { count: 25, tiles: [] },
    tn: { count: 991, tiles: [] },
  },
  histogram: {
    bins: 20, lo: 0.0021, hi: 0.9987,
    positive: Array.from({ length: 20 }, (_, i) => (i > 15 ? 40 : 0)),
    negative: Array.from({ length: 20 }, (_, i) => (i < 4 ? 200 : 0)),
    abstained: Array.from({ length: 20 }, () => 0),
  },
};

function renderPage(entry = '/new-dedup/tagging-bakeoff') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[entry]}>
        <NewDedupTaggingBakeoff />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.listBakeoffRuns).mockResolvedValue({ data: RUNS });
  vi.mocked(api.getBakeoffMetrics).mockResolvedValue({ data: METRICS });
  vi.mocked(api.getBakeoffImages).mockResolvedValue({ data: IMAGES });
  vi.mocked(api.getBakeoffBuckets).mockResolvedValue({ data: BUCKETS });
});

describe('<NewDedupTaggingBakeoff> — the run picker', () => {
  it('lists every run newest first with its label, date, status and sizes', async () => {
    renderPage();
    const picker = await screen.findByTestId('run-picker');
    const opts = within(picker).getAllByRole('option');
    expect(opts).toHaveLength(2);
    expect(opts[0]).toHaveTextContent('set-1 v1');
    expect(opts[0]).toHaveTextContent('ok');
    expect(opts[0]).toHaveTextContent('2 arms');
    expect(opts[0]).toHaveTextContent('2 heads');
    expect(opts[1]).toHaveTextContent('pilot');
    // Newest first is the API's order; the page must not re-sort it away.
    expect((picker as HTMLSelectElement).value).toBe('3');
  });

  it('picking a run puts it in the URL and re-asks for that run’s metrics', async () => {
    renderPage();
    const picker = await screen.findByTestId('run-picker');
    await userEvent.selectOptions(picker, '2');
    await waitFor(() => expect(api.getBakeoffMetrics).toHaveBeenCalledWith(2));
  });

  it('offers the dispatch instruction, not an empty screen, when there are no runs', async () => {
    vi.mocked(api.listBakeoffRuns).mockResolvedValue({ data: [] });
    renderPage();
    expect(await screen.findByTestId('no-runs')).toHaveTextContent('tagging_bakeoff');
  });
});

describe('<NewDedupTaggingBakeoff> — the metrics overview', () => {
  it('draws one row per head and one column per selected arm x mode', async () => {
    renderPage('/new-dedup/tagging-bakeoff?mode=pos_neg');
    const table = await screen.findByTestId('metrics-table');
    // Two heads, two arms, one mode = four cells, all present.
    expect(within(table).getByTestId('cell-19-7-pos_neg')).toBeInTheDocument();
    expect(within(table).getByTestId('cell-19-8-pos_neg')).toBeInTheDocument();
    expect(within(table).getByTestId('cell-22-7-pos_neg')).toBeInTheDocument();
    expect(within(table).getByTestId('cell-22-8-pos_neg')).toBeInTheDocument();
    // F1 leads, precision/recall follow, graded n is always there.
    const best = within(table).getByTestId('cell-19-7-pos_neg');
    expect(best).toHaveTextContent('0.92');
    expect(best).toHaveTextContent('P 0.94 R 0.89');
    expect(best).toHaveTextContent('n 1,235');
    expect(best).toHaveTextContent('best');
    expect(within(table).getByTestId('cell-19-8-pos_neg')).not.toHaveTextContent('best');
  });

  it('a null rate reads "nothing proposed" — never a zero — and keeps its graded n', async () => {
    renderPage('/new-dedup/tagging-bakeoff?mode=pos_neg&split=exam');
    const cell = await screen.findByTestId('cell-19-7-pos_neg');
    expect(cell).toHaveTextContent('nothing proposed');
    expect(cell).not.toHaveTextContent('0.00');
    // The graded n sits beside it, and the abstentions are counted separately.
    expect(screen.getByTestId('cell-19-7-pos_neg-graded')).toHaveTextContent('n 0');
    expect(screen.getByTestId('cell-19-7-pos_neg-graded')).toHaveTextContent('250 abst.');
    expect(cell.getAttribute('title')).toContain('NOT a score of zero');
  });

  it('a failed head is a decided cell carrying its own reason', async () => {
    renderPage('/new-dedup/tagging-bakeoff?mode=pos_neg');
    const cell = await screen.findByTestId('cell-22-7-pos_neg');
    expect(cell).toHaveTextContent('not trained');
    expect(cell).toHaveTextContent('too few positive listing-groups');
  });

  it('an arm x mode with no row at all renders an em dash, not a blank', async () => {
    // pos_only_centroid was never trained in this fixture.
    renderPage('/new-dedup/tagging-bakeoff?mode=pos_only_centroid');
    const cell = await screen.findByTestId('cell-19-7-pos_only_centroid');
    expect(cell).toHaveTextContent('—');
    expect(cell.getAttribute('title')).toContain('never trained this head');
  });

  it('the split switch swaps which half of the row is read', async () => {
    renderPage('/new-dedup/tagging-bakeoff?mode=pos_neg');
    expect(await screen.findByTestId('cell-19-8-pos_neg')).toHaveTextContent('0.80');
    await userEvent.click(screen.getByTestId('split-exam'));
    await waitFor(() =>
      expect(screen.getByTestId('cell-19-8-pos_neg')).toHaveTextContent('0.44'));
    expect(screen.getByTestId('cell-19-8-pos_neg')).toHaveTextContent('n 41');
  });

  it('deselecting an arm drops its columns', async () => {
    renderPage('/new-dedup/tagging-bakeoff?mode=pos_neg&arms=7,8');
    expect(await screen.findByTestId('cell-19-8-pos_neg')).toBeInTheDocument();
    await userEvent.click(screen.getByTestId('arm-8'));
    await waitFor(() =>
      expect(screen.queryByTestId('cell-19-8-pos_neg')).not.toBeInTheDocument());
    expect(screen.getByTestId('cell-19-7-pos_neg')).toBeInTheDocument();
  });
});

describe('<NewDedupTaggingBakeoff> — view A, photos across arms', () => {
  it('shows each photo once with a score strip per selected arm', async () => {
    renderPage('/new-dedup/tagging-bakeoff?mode=pos_neg&arms=7,8&tag=19');
    const tile = await screen.findByTestId('photo-555');
    // The SAME photo, both arms, aligned under one head column.
    expect(within(tile).getByTestId('score-7-pos_neg-19')).toBeInTheDocument();
    expect(within(tile).getByTestId('score-8-pos_neg-19')).toBeInTheDocument();
    // The human label rides as a glyph; the outcome and score live on the title.
    expect(within(tile).getByTestId('score-7-pos_neg-19')).toHaveTextContent('✓');
    expect(within(tile).getByTestId('score-7-pos_neg-19').getAttribute('title'))
      .toContain('Caught (tp)');
    expect(within(tile).getByTestId('score-8-pos_neg-19').getAttribute('title'))
      .toContain('Missed (fn)');
    expect(screen.getByTestId('photo-777')).toBeInTheDocument();
  });

  it('the outcome filter is unavailable until one head is picked, and says why', async () => {
    renderPage('/new-dedup/tagging-bakeoff?mode=pos_neg&tag=all');
    expect(await screen.findByTestId('outcome-needs-tag')).toBeInTheDocument();
    for (const o of ['tp', 'fp', 'fn', 'tn']) {
      expect(screen.getByTestId(`outcome-${o}`)).toBeDisabled();
    }
    // And it never reaches the API as a filter without its head.
    await waitFor(() => expect(api.getBakeoffImages).toHaveBeenCalled());
    expect(vi.mocked(api.getBakeoffImages).mock.calls[0][1]?.outcome).toBeUndefined();
  });

  it('with a head picked the outcome filter is live and travels to the API', async () => {
    renderPage('/new-dedup/tagging-bakeoff?mode=pos_neg&tag=19');
    const fp = await screen.findByTestId('outcome-fp');
    expect(fp).toBeEnabled();
    expect(fp).toHaveTextContent('Wrongly caught');
    await userEvent.click(fp);
    await waitFor(() => expect(api.getBakeoffImages).toHaveBeenLastCalledWith(
      3, expect.objectContaining({ outcome: 'fp', tag_id: 19, split: 'cv' }),
    ));
  });

  it('pages forward on the cursor the API returns, and back on its own stack', async () => {
    renderPage('/new-dedup/tagging-bakeoff?mode=pos_neg&tag=19');
    const next = await screen.findByTestId('page-next');
    expect(screen.getByTestId('page-prev')).toBeDisabled();
    await userEvent.click(next);
    await waitFor(() => expect(api.getBakeoffImages).toHaveBeenLastCalledWith(
      3, expect.objectContaining({ after_image_id: 777 }),
    ));
    expect(screen.getByTestId('page-prev')).toBeEnabled();
  });

  it('asks for one mode when one is selected and for all of them when several are', async () => {
    renderPage('/new-dedup/tagging-bakeoff?mode=pos_neg&tag=19');
    await waitFor(() => expect(api.getBakeoffImages).toHaveBeenLastCalledWith(
      3, expect.objectContaining({ mode: 'pos_neg' }),
    ));
    await userEvent.click(screen.getByTestId('mode-pos_only_centroid'));
    await waitFor(() => expect(
      vi.mocked(api.getBakeoffImages).mock.lastCall?.[1]?.mode,
    ).toBeUndefined());
  });
});

describe('<NewDedupTaggingBakeoff> — view B, one head’s buckets', () => {
  const entry = '/new-dedup/tagging-bakeoff?view=buckets&mode=pos_neg&arms=7&tag=19';

  it('names the four buckets in plain words with the codes secondary', async () => {
    renderPage(entry);
    expect(await screen.findByTestId('bucket-tp')).toHaveTextContent('Caught');
    expect(screen.getByTestId('bucket-fp')).toHaveTextContent('Wrongly caught');
    expect(screen.getByTestId('bucket-fn')).toHaveTextContent('Missed');
    expect(screen.getByTestId('bucket-tn')).toHaveTextContent('Correctly rejected');
    expect(screen.getByTestId('bucket-tp-count')).toHaveTextContent('206');
    expect(screen.getByTestId('bucket-fp-count')).toHaveTextContent('13');
  });

  it('draws the histogram and marks the threshold read off the metric row', async () => {
    // /buckets carries no threshold — it comes from the (arm, mode, head) metric.
    renderPage(entry);
    expect(await screen.findByTestId('histogram')).toBeInTheDocument();
    const mark = screen.getByTestId('threshold-mark');
    expect(mark.getAttribute('title')).toContain('Threshold 0.5');
  });

  it('leaves the threshold unmarked rather than guessed when the metric has none', async () => {
    renderPage('/new-dedup/tagging-bakeoff?view=buckets&mode=pos_neg&arms=7&tag=22');
    expect(await screen.findByTestId('histogram')).toBeInTheDocument();
    expect(screen.queryByTestId('threshold-mark')).not.toBeInTheDocument();
  });

  it('shows the abstained count beside the split only on the exam', async () => {
    renderPage(entry);
    expect(await screen.findByTestId('histogram')).toBeInTheDocument();
    expect(screen.queryByTestId('abstained-count')).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId('split-exam'));
    expect(await screen.findByTestId('abstained-count')).toHaveTextContent('12 abstained');
  });

  it('asks for exactly one arm, mode and head — and leaves the shared selection alone', async () => {
    renderPage('/new-dedup/tagging-bakeoff?view=buckets&mode=pos_neg&arms=7,8&tag=19');
    await waitFor(() => expect(api.getBakeoffBuckets).toHaveBeenLastCalledWith(
      3, expect.objectContaining({ arm_id: 7, mode: 'pos_neg', tag_id: 19, split: 'cv' }),
    ));
    // Narrowing view B to arm 8 must not unpick arm 7 from the matrix.
    await userEvent.selectOptions(screen.getByTestId('bucket-arm-picker'), '8');
    await waitFor(() => expect(api.getBakeoffBuckets).toHaveBeenLastCalledWith(
      3, expect.objectContaining({ arm_id: 8 }),
    ));
    expect(screen.getByTestId('cell-19-7-pos_neg')).toBeInTheDocument();
    expect(screen.getByTestId('cell-19-8-pos_neg')).toBeInTheDocument();
  });

  it('pages all four columns together', async () => {
    renderPage(entry);
    const next = await screen.findByTestId('bucket-next');
    expect(screen.getByTestId('bucket-prev')).toBeDisabled();
    await userEvent.click(next);
    await waitFor(() => expect(api.getBakeoffBuckets).toHaveBeenLastCalledWith(
      3, expect.objectContaining({ offset: 24, limit: 24 }),
    ));
  });
});

describe('<NewDedupTaggingBakeoff> — URL state', () => {
  it('a link reproduces the whole screen: run, view, arms, mode, tag and split', async () => {
    renderPage('/new-dedup/tagging-bakeoff?run=3&view=photos&arms=8&mode=pos_only_free_neg&tag=19&split=exam');
    await waitFor(() => expect(api.getBakeoffImages).toHaveBeenLastCalledWith(3, {
      split: 'exam',
      arms: '8',
      mode: 'pos_only_free_neg',
      tag_id: 19,
      outcome: undefined,
      after_image_id: undefined,
      limit: 60,
    }));
    expect(screen.getByTestId('arm-8')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTestId('arm-7')).toHaveAttribute('aria-pressed', 'false');
    expect(screen.getByTestId('mode-pos_only_free_neg')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTestId('split-exam')).toHaveAttribute('aria-pressed', 'true');
    expect((screen.getByTestId('tag-picker') as HTMLSelectElement).value).toBe('19');
  });

  it('switching view keeps the arms, mode, head and split that were chosen', async () => {
    renderPage('/new-dedup/tagging-bakeoff?arms=8&mode=pos_neg&tag=19&split=exam');
    await screen.findByTestId('photo-grid');
    await userEvent.click(screen.getByTestId('view-buckets'));
    await waitFor(() => expect(api.getBakeoffBuckets).toHaveBeenLastCalledWith(
      3, expect.objectContaining({ arm_id: 8, mode: 'pos_neg', tag_id: 19, split: 'exam' }),
    ));
    expect(screen.getByTestId('arm-8')).toHaveAttribute('aria-pressed', 'true');
  });
});
