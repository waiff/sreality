import { useMemo, useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';

import {
  getBakeoffBuckets,
  getBakeoffImages,
  getBakeoffMetrics,
  listBakeoffRuns,
  type BakeoffImageScore,
  type BakeoffMetric,
  type BakeoffMode,
  type BakeoffOutcome,
  type BakeoffSplit,
} from '@/lib/api';
import ErrorBanner from '@/components/ErrorBanner';
import ImageLightbox from '@/components/ImageLightbox';
import Spinner from '@/components/Spinner';
import { imageSrc } from '@/lib/imageUrl';
import type { ImagePublic } from '@/lib/types';

/* NEW DEDUP · Tagging bake-off — which encoder should the tagger buy into?
 *
 * ONE EXPERIMENT, THREE QUESTIONS, AND THE PAGE ANSWERS THEM IN THAT ORDER.
 * A run trains one yes/no classifier (a HEAD) per photo tag on the embeddings of
 * each encoder configuration (an ARM) under each training MODE, then scores every
 * labelled photo. So:
 *   1. the MATRIX — how did each arm do on each head? (the number)
 *   2. VIEW A     — what did the arms say about the SAME photograph? (the row)
 *   3. VIEW B     — what did one head get wrong, most-confident first? (the cell)
 * The operator moves number → photo → mistake without losing their selection:
 * arms, modes, head and split live in the URL and survive the view switch.
 *
 * TWO CONVENTIONS FROM THE CONTRACT ARE RENDERED, NOT ASSUMED (api/new_dedup_bakeoff.py):
 *   * a null precision/recall/f1 means NOTHING WAS PROPOSED — never zero. Every
 *     rate on this page is drawn beside its graded n for exactly that reason, and
 *     a null renders as an em dash that says so.
 *   * label: null (exam split only) is a human ABSTENTION. It carries a real
 *     score and a real prediction, and sits in no bucket and no rate — so it is
 *     shown as a count beside the split, never folded into one.
 * A `status: 'failed'` metric row is a DECIDED cell, not a missing one: it renders
 * struck, carrying its own note.
 *
 * SCORES COMPARE ONLY WITHIN ONE MODE. The two logistic modes score in [0, 1];
 * pos_only_centroid is a cosine in [-1, 1]. Every score bar here is normalised by
 * its own mode's scale and the centroid mode says "cosine" wherever it appears,
 * so no bar invites a comparison the numbers cannot support.
 *
 * THE CONFUSION SQUARE is this page's one repeated glyph: a 2x2 where the top row
 * is "the model said yes" and the left column is "you said yes". It sits in every
 * matrix cell (so an F1 of 0.9 can never hide a head that fired twice), labels
 * View B's four bucket columns, and colours every outcome mark in View A. Same
 * quadrant, same colour, everywhere.
 *
 * WHAT THE CONTRACT DOES NOT GIVE, AND WHAT IS DONE INSTEAD:
 *   * /buckets carries no threshold — it is read off the metric row for the same
 *     (arm, mode, head), which this page already holds. No metric row (a run
 *     still going, a failed head) means no threshold marker, not a guessed one.
 *   * a head's human label arrives only on a metric row, so a run with no metrics
 *     yet gets a head picker that names them by tag id.
 *   * /images pages forward only (`next_after_image_id`, no inverse), so
 *     "previous" is a cursor stack this page keeps.
 */

/* ---------------------------------------------------------------- vocabulary */

const MODES: readonly BakeoffMode[] = ['pos_neg', 'pos_only_free_neg', 'pos_only_centroid'];

/* Plain words first — the mode names are the experiment's jargon, and the
 * operator reads this page to make a decision, not to learn them. */
const MODE_LABEL: Record<BakeoffMode, string> = {
  pos_neg: 'Yes + no',
  pos_only_free_neg: 'Yes only, borrowed no',
  pos_only_centroid: 'Closeness only',
};
const MODE_HELP: Record<BakeoffMode, string> = {
  pos_neg:
    'pos_neg — trained on the photos you marked YES and the ones you marked NO. The full-diet reading.',
  pos_only_free_neg:
    'pos_only_free_neg — trained on your YES photos only; the other heads’ yes photos stand in as the no side, for free. This is the one that answers "what did marking negatives actually buy me?"',
  pos_only_centroid:
    'pos_only_centroid — no classifier at all: just how close a photo sits to the average of your yes photos. The strict positive-only reading. Its score is a cosine (−1…1), not a probability, so it is never comparable with the other two.',
};

const SPLITS: readonly BakeoffSplit[] = ['cv', 'exam'];
const SPLIT_LABEL: Record<BakeoffSplit, string> = {
  cv: 'Cross-validation',
  exam: 'Exam',
};
const SPLIT_HELP: Record<BakeoffSplit, string> = {
  cv: 'Every training photo, scored by a copy of the head that never saw it (grouped folds, so one listing’s photos never straddle the divide). Big numbers, but the labels are your own.',
  exam: 'The sealed 250-photo holdout, scored by the head refit on everything. Thin per head, but it is the only material the head never consumed.',
};

const OUTCOMES: readonly BakeoffOutcome[] = ['tp', 'fp', 'fn', 'tn'];
/* Plain words lead, the code follows in small type — the operator thinks in
 * "wrongly caught", the API and the ledger think in "fp". */
const OUTCOME_WORD: Record<BakeoffOutcome, string> = {
  tp: 'Caught',
  fp: 'Wrongly caught',
  fn: 'Missed',
  tn: 'Correctly rejected',
};
const OUTCOME_HELP: Record<BakeoffOutcome, string> = {
  tp: 'The model said yes and you said yes.',
  fp: 'The model said yes and you said no. These are the ones that would pollute the tag.',
  fn: 'The model said no and you said yes. These are the ones it would quietly lose.',
  tn: 'The model said no and you said no.',
};
const OUTCOME_COLOR: Record<BakeoffOutcome, string> = {
  tp: 'var(--color-sage)',
  fp: 'var(--color-brick)',
  fn: 'var(--color-ochre)',
  tn: 'var(--color-ink-4)',
};
const ABSTAINED_HELP =
  'You abstained on this photo in the exam — an explicit leave-out, a can’t-tell, or a declared default you never touched. It still carries a score and a prediction, but it is in no bucket and in no rate.';

/* ------------------------------------------------------------------ helpers */

const fmt2 = (v: number | null | undefined): string => (v == null ? '—' : v.toFixed(2));
const fmtN = (n: number): string => n.toLocaleString();

/* "interier - kuchyně" reads as "kuchyně" in a column head six characters wide.
 * The full label stays on the title attribute wherever this is used. */
const shortHead = (label: string): string => {
  const parts = label.split(/\s+[-–]\s+/);
  return parts[parts.length - 1] || label;
};

/* The centroid mode's cosine lives in [-1, 1]; everything else in [0, 1]. A bar
 * drawn on the wrong scale is a lie about a number the operator cannot re-derive. */
const barFraction = (score: number, mode: BakeoffMode): number => {
  const f = mode === 'pos_only_centroid' ? (score + 1) / 2 : score;
  return Math.max(0, Math.min(1, f));
};
const scoreUnit = (mode: BakeoffMode): string =>
  mode === 'pos_only_centroid' ? 'cosine' : 'probability';

/* One metric row has two faces — the split picks which. Written once so the
 * matrix, its tooltip and View B cannot read a different half of the same row. */
interface Face {
  f1: number | null;
  precision: number | null;
  recall: number | null;
  graded: number;
  tp: number;
  fp: number;
  fn: number;
  tn: number;
  abstained: number | null;
}
const face = (m: BakeoffMetric, split: BakeoffSplit): Face =>
  split === 'cv'
    ? {
      f1: m.cv_f1, precision: m.cv_precision, recall: m.cv_recall,
      graded: m.cv_graded_n, tp: m.cv_tp, fp: m.cv_fp, fn: m.cv_fn, tn: m.cv_tn,
      abstained: null,
    }
    : {
      f1: m.exam_f1, precision: m.exam_precision, recall: m.exam_recall,
      graded: m.exam_graded_n, tp: m.exam_tp, fp: m.exam_fp, fn: m.exam_fn,
      tn: m.exam_tn, abstained: m.exam_abstained_n,
    };

const NOTHING_PROPOSED =
  'Nothing was proposed — the head never fired on these photos, so there is no rate to compute. This is NOT a score of zero.';

const cellTitle = (m: BakeoffMetric, split: BakeoffSplit): string => {
  if (m.status === 'failed') {
    return `Could not be trained or graded. ${m.note ?? 'No reason recorded.'}`;
  }
  const f = face(m, split);
  const head = f.f1 == null
    ? NOTHING_PROPOSED
    : `F1 ${f.f1.toFixed(3)} · precision ${fmt2(f.precision)} · recall ${fmt2(f.recall)}`;
  const conf = `caught ${f.tp} · wrongly caught ${f.fp} · missed ${f.fn} · correctly rejected ${f.tn}`;
  const rest = [
    `graded ${fmtN(f.graded)} photo${f.graded === 1 ? '' : 's'}`,
    f.abstained ? `${fmtN(f.abstained)} abstained (in no rate)` : null,
    conf,
    m.threshold == null ? null : `threshold ${m.threshold}`,
    `trained on ${fmtN(m.n_pos)} yes / ${fmtN(m.n_neg)} no across ${fmtN(m.n_groups)} listings`,
  ].filter(Boolean).join(' · ');
  return `${head}\n${rest}`;
};

/* ------------------------------------------------------- the confusion square */

/* The page's one repeated glyph. Top row = the model said yes; left column = you
 * said yes. Quadrant opacity is that quadrant's share of the four, so a head that
 * scores well by never firing looks visibly different from one that scores well by
 * being right. `only` lights a single quadrant (View B's column headers). */
function ConfusionSquare({
  tp, fp, fn, tn, size = 14, only,
}: {
  tp: number; fp: number; fn: number; tn: number; size?: number; only?: BakeoffOutcome;
}) {
  const cells: ReadonlyArray<[BakeoffOutcome, number, number, number]> = [
    ['tp', tp, 0, 0], ['fp', fp, 1, 0], ['fn', fn, 0, 1], ['tn', tn, 1, 1],
  ];
  const max = Math.max(tp, fp, fn, tn, 1);
  const s = size / 2;
  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      aria-hidden
      className="shrink-0"
      data-testid="confusion-square"
    >
      {cells.map(([o, v, x, y]) => (
        <rect
          key={o}
          x={x * s + 0.4}
          y={y * s + 0.4}
          width={s - 0.8}
          height={s - 0.8}
          fill={OUTCOME_COLOR[o]}
          opacity={only ? (only === o ? 0.85 : 0.1) : 0.12 + 0.7 * (v / max)}
        />
      ))}
    </svg>
  );
}

/* ----------------------------------------------------------------- the matrix */

/* One (head x arm x mode) cell. F1 leads because it is the one number that
 * answers "which arm"; precision and recall sit under it so a lopsided head is
 * visible without reading the tooltip; the graded n is ALWAYS present, because
 * the contract says a rate without it is unreadable. */
function MetricCell({
  metric, split, best, testId,
}: {
  metric: BakeoffMetric | undefined; split: BakeoffSplit; best: boolean; testId: string;
}) {
  if (!metric) {
    return (
      <td
        data-testid={testId}
        title="This arm never trained this head in this mode."
        className="px-2 py-1.5 text-center text-[var(--color-ink-4)] border-l border-[var(--color-rule-soft)]"
      >
        —
      </td>
    );
  }
  if (metric.status === 'failed') {
    return (
      <td
        data-testid={testId}
        title={cellTitle(metric, split)}
        className="px-2 py-1.5 border-l border-[var(--color-rule-soft)] bg-[var(--color-brick-soft)]"
      >
        <span className="text-[0.7rem] text-[var(--color-brick)] line-through">not trained</span>
        <span className="block text-[0.58rem] text-[var(--color-ink-3)] leading-tight max-w-[9rem] truncate">
          {metric.note ?? 'no reason recorded'}
        </span>
      </td>
    );
  }
  const f = face(metric, split);
  /* Copper, one accent, alpha-ramped by F1 — a rainbow would say "these are
   * different kinds of thing" when they are one thing at different strengths. */
  const tint = f.f1 == null ? 0 : 5 + Math.round(f.f1 * 30);
  return (
    <td
      data-testid={testId}
      title={cellTitle(metric, split)}
      style={{ background: tint ? `color-mix(in srgb, var(--color-copper) ${tint}%, transparent)` : undefined }}
      className={`px-2 py-1.5 border-l border-[var(--color-rule-soft)] ${
        best ? 'outline outline-1 -outline-offset-1 outline-[var(--color-copper)]' : ''
      }`}
    >
      <div className="flex items-center gap-1.5">
        <span
          className={`font-mono tabular-nums text-[0.82rem] ${
            f.f1 == null ? 'text-[var(--color-ink-4)]' : 'text-[var(--color-ink)]'
          }`}
        >
          {fmt2(f.f1)}
        </span>
        {best && f.f1 != null && (
          <span className="text-[0.55rem] uppercase tracking-[0.12em] text-[var(--color-copper)]">best</span>
        )}
        <span className="ml-auto">
          <ConfusionSquare tp={f.tp} fp={f.fp} fn={f.fn} tn={f.tn} />
        </span>
      </div>
      {f.f1 == null ? (
        <span className="block text-[0.58rem] leading-tight text-[var(--color-ink-3)]">
          nothing proposed
        </span>
      ) : (
        <span className="block text-[0.58rem] leading-tight text-[var(--color-ink-3)] font-mono tabular-nums">
          P {fmt2(f.precision)} R {fmt2(f.recall)}
        </span>
      )}
      <span
        className="block text-[0.58rem] leading-tight text-[var(--color-ink-4)] font-mono tabular-nums"
        data-testid={`${testId}-graded`}
      >
        n {fmtN(f.graded)}{f.abstained ? ` · ${fmtN(f.abstained)} abst.` : ''}
      </span>
    </td>
  );
}

/* -------------------------------------------------------------- the histogram */

/* Twenty bins over the cell's MEASURED range, stacked by what you said, with the
 * head's threshold marked. Hand-drawn SVG rather than a chart library: there is
 * no time domain here, no shared axis to inherit, and nothing to resize-observe. */
function ScoreHistogram({
  hist, threshold, mode,
}: {
  hist: { bins: number; lo: number; hi: number; positive: number[]; negative: number[]; abstained: number[] };
  threshold: number | null;
  mode: BakeoffMode;
}) {
  const n = Math.max(1, hist.bins);
  const totals = Array.from({ length: n }, (_, i) =>
    (hist.negative[i] ?? 0) + (hist.positive[i] ?? 0) + (hist.abstained[i] ?? 0));
  const max = Math.max(1, ...totals);
  const W = 320;
  const H = 76;
  const bw = W / n;
  const span = hist.hi - hist.lo;
  const tPct = threshold == null || span <= 0
    ? null
    : Math.max(0, Math.min(1, (threshold - hist.lo) / span)) * 100;
  return (
    <div data-testid="histogram">
      <div className="relative">
        <svg
          viewBox={`0 0 ${W} ${H}`}
          width="100%"
          height={H}
          preserveAspectRatio="none"
          role="img"
          aria-label={`Score distribution in ${n} bins, from ${hist.lo.toFixed(3)} to ${hist.hi.toFixed(3)}`}
        >
          {Array.from({ length: n }, (_, i) => {
            const h = (v: number) => (v / max) * (H - 2);
            const bars: ReactNode[] = [];
            let y = H;
            const push = (v: number, fill: string, op: number, key: string) => {
              if (v <= 0) return;
              y -= h(v);
              bars.push(
                <rect key={key} x={i * bw + 0.3} y={y} width={bw - 0.6} height={h(v)} fill={fill} opacity={op} />,
              );
            };
            push(hist.negative[i] ?? 0, OUTCOME_COLOR.tn, 0.55, `n${i}`);
            push(hist.positive[i] ?? 0, OUTCOME_COLOR.tp, 0.85, `p${i}`);
            push(hist.abstained[i] ?? 0, 'var(--color-ink-3)', 0.45, `a${i}`);
            return <g key={i}>{bars}</g>;
          })}
        </svg>
        {tPct != null && (
          <span
            data-testid="threshold-mark"
            title={`Threshold ${threshold} — at or above this score the head says yes.`}
            className="absolute top-0 bottom-0 border-l border-dashed border-[var(--color-copper)] pointer-events-none"
            style={{ left: `${tPct}%` }}
          />
        )}
      </div>
      <div className="mt-0.5 flex justify-between gap-2 text-[0.6rem] font-mono tabular-nums text-[var(--color-ink-4)]">
        <span>{hist.lo.toFixed(3)}</span>
        <span className="font-sans text-[var(--color-ink-3)]">
          {n} bins over the measured {scoreUnit(mode)} range
          {threshold == null ? ' · no threshold recorded' : ` · threshold ${threshold}`}
        </span>
        <span>{hist.hi.toFixed(3)}</span>
      </div>
      <p className="mt-1 flex flex-wrap gap-3 text-[0.6rem] text-[var(--color-ink-3)]">
        <span className="flex items-center gap-1">
          <i className="inline-block w-2.5 h-2.5 rounded-[1px]" style={{ background: OUTCOME_COLOR.tp, opacity: 0.85 }} />
          you said yes
        </span>
        <span className="flex items-center gap-1">
          <i className="inline-block w-2.5 h-2.5 rounded-[1px]" style={{ background: OUTCOME_COLOR.tn, opacity: 0.55 }} />
          you said no
        </span>
        <span className="flex items-center gap-1 cursor-help" title={ABSTAINED_HELP}>
          <i className="inline-block w-2.5 h-2.5 rounded-[1px]" style={{ background: 'var(--color-ink-3)', opacity: 0.45 }} />
          you abstained
        </span>
      </p>
    </div>
  );
}

/* ------------------------------------------------------------- small controls */

function Chip({
  on, onClick, children, title, disabled = false, testId,
}: {
  on: boolean; onClick: () => void; children: ReactNode;
  title?: string; disabled?: boolean; testId?: string;
}) {
  return (
    <button
      type="button"
      title={title}
      data-testid={testId}
      aria-pressed={on}
      disabled={disabled}
      onClick={onClick}
      className={`px-2 py-1 text-xs rounded-[var(--radius-sm)] border transition-colors ${
        disabled
          ? 'border-[var(--color-rule)] text-[var(--color-ink-4)] opacity-50 cursor-not-allowed'
          : on
            ? 'border-[var(--color-copper)] bg-[var(--color-copper-soft)] text-[var(--color-copper)]'
            : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink)] hover:border-[var(--color-rule-strong)]'
      }`}
    >
      {children}
    </button>
  );
}

function Caption({ children }: { children: ReactNode }) {
  return (
    <span className="text-[0.62rem] tracking-[0.16em] uppercase text-[var(--color-ink-4)]">
      {children}
    </span>
  );
}

const selectClass =
  'px-2 py-1 text-sm rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-transparent text-[var(--color-ink)]';

/* The shared viewer takes images_public rows; a bake-off tile already carries the
 * only two fields imageSrc reads, and every badge the viewer draws self-guards on
 * null — so the row is widened rather than re-fetched. */
const asImagePublic = (t: { image_id: number; storage_path: string | null }): ImagePublic => ({
  id: t.image_id,
  sreality_id: 0,
  sequence: null,
  sreality_url: '',
  storage_path: t.storage_path,
  clip_fine_tag: null,
  clip_logical_tag: null,
  clip_confidence: null,
  clip_render_score: null,
  phash: null,
});

/* ==========================================================================
 * The page
 * ======================================================================== */

const PAGE_A = 60;
const PAGE_B = 24;

export default function NewDedupTaggingBakeoff() {
  const [params, setParams] = useSearchParams();
  const [lightbox, setLightbox] = useState<{ images: ImagePublic[]; index: number } | null>(null);
  /* Forward-only cursor paging (the contract gives `next_after_image_id` and no
   * inverse), so "previous" is a stack this page keeps. */
  const [cursors, setCursors] = useState<number[]>([]);

  const patch = (next: Record<string, string | null>) => {
    const merged = new URLSearchParams(params);
    for (const [k, v] of Object.entries(next)) {
      if (v === null || v === '') merged.delete(k);
      else merged.set(k, v);
    }
    setParams(merged, { replace: true });
  };

  const runsQ = useQuery({ queryKey: ['bakeoff-runs'], queryFn: () => listBakeoffRuns() });
  const runs = useMemo(() => runsQ.data?.data ?? [], [runsQ.data]);

  const runId = Number(params.get('run') ?? 0) || runs[0]?.id || null;
  const run = runs.find((r) => r.id === runId) ?? null;

  const metricsQ = useQuery({
    queryKey: ['bakeoff-metrics', runId],
    queryFn: () => getBakeoffMetrics(runId as number),
    enabled: runId != null,
  });
  const metrics = useMemo(() => metricsQ.data?.data ?? [], [metricsQ.data]);

  /* A head carries its human label only on a metric row — the run itself lists
   * tag ids. A run whose metrics have not landed yet still gets a picker, just
   * one that names the heads by id. */
  const heads = useMemo(() => {
    const byId = new Map<number, string>();
    for (const m of metrics) if (!byId.has(m.tag_id)) byId.set(m.tag_id, m.tag_label);
    for (const id of run?.heads ?? []) if (!byId.has(id)) byId.set(id, `tag ${id}`);
    return [...byId.entries()]
      .map(([id, label]) => ({ id, label }))
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [metrics, run]);

  const arms = useMemo(() => run?.arms ?? [], [run]);

  /* Selections. A default derived from the data is NOT written into the URL —
   * a shared link then says only what the operator actually chose. */
  const armIds = useMemo(() => {
    const raw = (params.get('arms') ?? '').split(',').map(Number)
      .filter((n) => Number.isFinite(n) && n > 0);
    const known = raw.filter((id) => arms.some((a) => a.id === id));
    return known.length ? known : arms.slice(0, 2).map((a) => a.id);
  }, [params, arms]);

  const modeSel = useMemo(() => {
    const raw = (params.get('mode') ?? '').split(',')
      .filter((m): m is BakeoffMode => (MODES as readonly string[]).includes(m));
    return raw.length ? raw : [...MODES];
  }, [params]);

  const split: BakeoffSplit = params.get('split') === 'exam' ? 'exam' : 'cv';
  const view = params.get('view') === 'buckets' ? 'buckets' : 'photos';

  /* 'all' is a real choice in View A, and the reason the outcome filter can be
   * unavailable — it is not the absence of a choice. View B has no such option:
   * a bucket is one head's, so 'all' there resolves to the first head. */
  const rawTag = params.get('tag');
  const tagId = rawTag === 'all' && view === 'photos'
    ? null
    : (Number(rawTag ?? 0) || heads[0]?.id || null);

  const toggleArm = (id: number) => {
    const next = armIds.includes(id) ? armIds.filter((a) => a !== id) : [...armIds, id];
    setCursors([]);
    patch({ arms: next.length ? next.join(',') : String(id), after: null });
  };
  const toggleMode = (m: BakeoffMode) => {
    const next = modeSel.includes(m) ? modeSel.filter((x) => x !== m) : [...modeSel, m];
    setCursors([]);
    patch({ mode: next.length ? next.join(',') : m, after: null });
  };

  /* The matrix: one row per head, one column per (arm x mode) selected. */
  const metricAt = useMemo(() => {
    const m = new Map<string, BakeoffMetric>();
    for (const row of metrics) m.set(`${row.tag_id}|${row.arm_id}|${row.mode}`, row);
    return m;
  }, [metrics]);

  const columns = useMemo(
    () => armIds.flatMap((armId) => modeSel.map((mode) => ({ armId, mode }))),
    [armIds, modeSel],
  );

  /* "Best arm per head, at a glance" — the top F1 in the row, on the split shown.
   * A tie marks every winner; picking one arbitrarily would read as a decision. */
  const bestPerHead = useMemo(() => {
    const best = new Map<number, number>();
    for (const h of heads) {
      let top = -1;
      for (const c of columns) {
        const m = metricAt.get(`${h.id}|${c.armId}|${c.mode}`);
        if (!m || m.status === 'failed') continue;
        const v = face(m, split).f1;
        if (v != null && v > top) top = v;
      }
      if (top >= 0) best.set(h.id, top);
    }
    return best;
  }, [heads, columns, metricAt, split]);

  /* ------------------------------------------------------------- view A data */

  const outcome = (params.get('outcome') ?? '') as BakeoffOutcome | '';
  /* The contract: an outcome is one head's verdict, so it needs a head. */
  const outcomeOk = tagId != null;
  const after = Number(params.get('after') ?? 0) || null;

  const imagesQ = useQuery({
    queryKey: ['bakeoff-images', runId, split, armIds.join(','), modeSel.join(','), tagId, outcome, after],
    queryFn: () => getBakeoffImages(runId as number, {
      split,
      arms: armIds.join(','),
      /* The route takes ONE mode; with several selected we ask for all of them
       * and narrow on the client, so a tile can hold the mode comparison too. */
      mode: modeSel.length === 1 ? modeSel[0] : undefined,
      tag_id: tagId ?? undefined,
      outcome: outcomeOk && outcome ? outcome : undefined,
      after_image_id: after ?? undefined,
      limit: PAGE_A,
    }),
    enabled: runId != null && view === 'photos',
  });
  const images = useMemo(() => imagesQ.data?.data.images ?? [], [imagesQ.data]);
  const nextAfter = imagesQ.data?.data.next_after_image_id ?? null;
  const galleryA = useMemo(() => images.map(asImagePublic), [images]);

  /* Which heads a tile shows, in ONE order, so the columns line up across arms. */
  const tileHeads = useMemo(() => {
    if (tagId != null) {
      const h = heads.find((x) => x.id === tagId);
      return h ? [h] : [];
    }
    const seen = new Map<number, string>();
    for (const img of images) {
      for (const s of img.scores) if (!seen.has(s.tag_id)) seen.set(s.tag_id, s.tag_label);
    }
    return [...seen.entries()].map(([id, label]) => ({ id, label }))
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [images, tagId, heads]);

  /* ------------------------------------------------------------- view B data */

  /* View B looks at ONE cell. Its arm and mode default to the first of the shared
   * selection and are stored under their own keys, so narrowing here never throws
   * away the multi-arm selection the matrix and View A are using. */
  const bArmId = Number(params.get('barm') ?? 0) || armIds[0] || null;
  const rawBMode = params.get('bmode');
  const bMode: BakeoffMode = rawBMode && (MODES as readonly string[]).includes(rawBMode)
    ? (rawBMode as BakeoffMode)
    : (modeSel[0] ?? 'pos_neg');
  const offset = Math.max(0, Number(params.get('off') ?? 0) || 0);

  const bucketsQ = useQuery({
    queryKey: ['bakeoff-buckets', runId, bArmId, bMode, tagId, split, offset],
    queryFn: () => getBakeoffBuckets(runId as number, {
      arm_id: bArmId as number, mode: bMode, tag_id: tagId as number,
      split, limit: PAGE_B, offset,
    }),
    enabled: runId != null && view === 'buckets' && bArmId != null && tagId != null,
  });
  const buckets = bucketsQ.data?.data ?? null;
  /* The buckets payload carries no threshold — it belongs to the metric row for
   * the same (arm, mode, head), which this page already holds. */
  const bMetric = metrics.find(
    (m) => m.arm_id === bArmId && m.mode === bMode && m.tag_id === tagId,
  ) ?? null;

  /* ------------------------------------------------------------------ render */

  if (runsQ.isLoading) return <div className="p-6"><Spinner /></div>;
  if (runsQ.error) return <div className="p-6"><ErrorBanner message={(runsQ.error as Error).message} /></div>;

  if (runs.length === 0) {
    return (
      <div className="max-w-[60rem] mx-auto px-4 py-10">
        <h1 className="text-lg font-medium text-[var(--color-ink)]">Tagging bake-off</h1>
        <p className="mt-3 text-sm text-[var(--color-ink-2)] max-w-prose" data-testid="no-runs">
          No runs yet &mdash; dispatch the <b>tagging_bakeoff</b> lane. A run trains one yes/no
          classifier per tag on every encoder configuration in the set, scores every labelled
          photo with it, and this page is where those results are compared.
        </p>
      </div>
    );
  }

  const armName = (id: number | null) =>
    arms.find((a) => a.id === id)?.arm ?? (id == null ? '—' : `arm ${id}`);
  const headName = (id: number | null) =>
    heads.find((h) => h.id === id)?.label ?? (id == null ? 'all heads' : `tag ${id}`);

  return (
    <div className="max-w-[112rem] mx-auto px-4 py-6">
      {/* ------------------------------------------------------------ header */}
      <header className="border-b border-[var(--color-rule)] pb-3">
        <h1 className="text-lg font-medium text-[var(--color-ink)]">Tagging bake-off</h1>
        <p className="mt-0.5 max-w-prose text-xs text-[var(--color-ink-3)]">
          One yes/no classifier &mdash; a <b>head</b> &mdash; per photo tag, trained on the
          picture-numbers of each encoder configuration &mdash; an <b>arm</b> &mdash; under each
          training <b>mode</b>, then asked about every photo you have labelled. Read it in three
          steps: the table says which arm did best on which tag; <b>Photos</b> shows what every arm
          said about the same photograph; <b>By head</b> shows what one head actually got wrong.
        </p>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Caption>run</Caption>
          <select
            aria-label="Bake-off run"
            data-testid="run-picker"
            className={selectClass}
            value={runId ?? ''}
            onChange={(e) => { setCursors([]); patch({ run: e.target.value, after: null, off: null }); }}
          >
            {runs.map((r) => (
              <option key={r.id} value={r.id}>
                {r.label ?? `run ${r.id}`} · {new Date(r.created_at).toLocaleDateString()} · {r.status}
                {' · '}{r.arms.length} arm{r.arms.length === 1 ? '' : 's'}
                {' · '}{r.heads.length} head{r.heads.length === 1 ? '' : 's'}
              </option>
            ))}
          </select>
          {run?.note && <span className="text-xs text-[var(--color-ink-3)]">{run.note}</span>}
          {run?.min_train_positives != null && (
            <span
              className="text-[0.7rem] text-[var(--color-ink-4)]"
              title="A head with fewer of your yes photos than this was not trained at all."
            >
              min {run.min_train_positives} yes photos per head
            </span>
          )}
        </div>

        <div className="mt-2 flex flex-wrap items-start gap-x-4 gap-y-2">
          <span className="flex flex-wrap items-center gap-1" role="group" aria-label="Arms">
            <Caption>arms</Caption>
            {arms.map((a) => (
              <Chip
                key={a.id}
                testId={`arm-${a.id}`}
                on={armIds.includes(a.id)}
                onClick={() => toggleArm(a.id)}
                title={`${a.model}${a.revision ? ` @ ${a.revision.slice(0, 8)}` : ''} · ${a.library ?? 'library ?'} · ${a.pooling ?? 'pooling ?'} pooling · ${a.resolution ?? '?'}px · ${a.preprocessing ?? 'preprocessing ?'} · ${a.dtype ?? 'dtype ?'} · ${a.dim ?? '?'} dims · ${a.status}${a.note ? ` — ${a.note}` : ''}`}
              >
                {a.arm}
              </Chip>
            ))}
          </span>

          <span className="flex flex-wrap items-center gap-1" role="group" aria-label="Training modes">
            <Caption>trained on</Caption>
            {MODES.map((m) => (
              <Chip
                key={m}
                testId={`mode-${m}`}
                on={modeSel.includes(m)}
                onClick={() => toggleMode(m)}
                title={MODE_HELP[m]}
              >
                {MODE_LABEL[m]}
              </Chip>
            ))}
          </span>

          <span className="flex flex-wrap items-center gap-1" role="group" aria-label="Which numbers">
            <Caption>numbers from</Caption>
            {SPLITS.map((s) => (
              <Chip
                key={s}
                testId={`split-${s}`}
                on={split === s}
                onClick={() => { setCursors([]); patch({ split: s, after: null, off: null }); }}
                title={SPLIT_HELP[s]}
              >
                {SPLIT_LABEL[s]}
              </Chip>
            ))}
          </span>
        </div>
      </header>

      {/* ------------------------------------------------------- the matrix */}
      <section className="mt-5">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-sm font-medium text-[var(--color-ink)]">How each arm did, head by head</h2>
          <p className="max-w-prose text-[0.7rem] text-[var(--color-ink-3)]">
            Big number = <b>F1</b>, one score balancing &ldquo;does it catch them&rdquo; against
            &ldquo;is it right when it fires&rdquo;. <b>P</b> precision, <b>R</b> recall,
            <b> n</b> how many photos were graded. The little square is the four outcomes at a
            glance: top row = the model said yes, left column = you said yes.
          </p>
        </div>

        {metricsQ.isLoading ? (
          <div className="py-8 flex justify-center"><Spinner /></div>
        ) : metricsQ.error ? (
          <div className="mt-2"><ErrorBanner message={(metricsQ.error as Error).message} /></div>
        ) : metrics.length === 0 ? (
          <p className="mt-4 text-sm text-[var(--color-ink-2)]" data-testid="no-metrics">
            This run has no results yet. It is <b>{run?.status}</b> &mdash; heads appear here as they finish.
          </p>
        ) : (
          <div className="mt-2 overflow-x-auto rounded-[var(--radius-sm)] border border-[var(--color-rule)]">
            <table className="min-w-full text-xs" data-testid="metrics-table">
              <thead>
                <tr className="bg-[var(--color-paper-2)]">
                  <th className="sticky left-0 z-10 bg-[var(--color-paper-2)] px-2 py-1.5 text-left font-medium text-[var(--color-ink-2)]">
                    head
                  </th>
                  {columns.map((c) => (
                    <th
                      key={`${c.armId}|${c.mode}`}
                      className="border-l border-[var(--color-rule-soft)] px-2 py-1.5 text-left font-medium"
                      title={MODE_HELP[c.mode]}
                    >
                      <span className="block text-[var(--color-ink)]">{armName(c.armId)}</span>
                      <span className="block text-[0.6rem] font-normal text-[var(--color-ink-3)]">
                        {MODE_LABEL[c.mode]}
                      </span>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {heads.map((h) => (
                  <tr key={h.id} className="border-t border-[var(--color-rule-soft)]">
                    <th
                      scope="row"
                      title={h.label}
                      className="sticky left-0 z-10 whitespace-nowrap bg-[var(--color-paper)] px-2 py-1.5 text-left font-normal text-[var(--color-ink)]"
                    >
                      {shortHead(h.label)}
                    </th>
                    {columns.map((c) => {
                      const m = metricAt.get(`${h.id}|${c.armId}|${c.mode}`);
                      const f1 = m && m.status !== 'failed' ? face(m, split).f1 : null;
                      const top = bestPerHead.get(h.id);
                      return (
                        <MetricCell
                          key={`${c.armId}|${c.mode}`}
                          testId={`cell-${h.id}-${c.armId}-${c.mode}`}
                          metric={m}
                          split={split}
                          best={f1 != null && top != null && f1 === top && columns.length > 1}
                        />
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="mt-1.5 text-[0.7rem] text-[var(--color-ink-3)]">
          A cell reading <b>&mdash;</b> with &ldquo;nothing proposed&rdquo; means the head never
          fired on those photos, so there is no rate to compute &mdash; it is not a score of zero.
          A <b>not trained</b> cell is a decided outcome carrying its own reason, not a gap.
          {split === 'exam' && ' On the exam, “abst.” counts the photos you abstained on: they are in no rate.'}
        </p>
      </section>

      {/* ------------------------------------------------------- view switch */}
      <div className="mt-6 flex flex-wrap items-center gap-2 border-b border-[var(--color-rule)] pb-2">
        <Chip
          testId="view-photos"
          on={view === 'photos'}
          onClick={() => patch({ view: 'photos' })}
          title="A page of photographs, each carrying what every selected arm said about it."
        >
          Photos, across arms
        </Chip>
        <Chip
          testId="view-buckets"
          on={view === 'buckets'}
          onClick={() => patch({ view: 'buckets' })}
          title="One head under one arm, split into what it caught, wrongly caught, missed and correctly rejected."
        >
          By head: right and wrong
        </Chip>

        <span className="ml-2 flex items-center gap-1">
          <Caption>head</Caption>
          <select
            aria-label="Head"
            data-testid="tag-picker"
            className={selectClass}
            value={tagId == null ? 'all' : String(tagId)}
            onChange={(e) => {
              setCursors([]);
              patch({ tag: e.target.value, after: null, off: null, outcome: null });
            }}
          >
            {view === 'photos' && <option value="all">All heads</option>}
            {heads.map((h) => <option key={h.id} value={h.id}>{h.label}</option>)}
          </select>
        </span>
      </div>

      {/* ------------------------------------------------------------ view A */}
      {view === 'photos' && (
        <section className="mt-4">
          <div className="flex flex-wrap items-center gap-2">
            <Caption>outcome</Caption>
            <Chip
              testId="outcome-any"
              on={!outcome}
              disabled={!outcomeOk}
              onClick={() => { setCursors([]); patch({ outcome: null, after: null }); }}
            >
              any
            </Chip>
            {OUTCOMES.map((o) => (
              <Chip
                key={o}
                testId={`outcome-${o}`}
                on={outcome === o}
                disabled={!outcomeOk}
                title={outcomeOk ? OUTCOME_HELP[o] : 'Pick one head first — an outcome is one head’s verdict.'}
                onClick={() => { setCursors([]); patch({ outcome: o, after: null }); }}
              >
                {OUTCOME_WORD[o]} <span className="text-[0.6rem] text-[var(--color-ink-4)]">{o}</span>
              </Chip>
            ))}
            {!outcomeOk && (
              <span className="text-[0.7rem] text-[var(--color-ink-3)]" data-testid="outcome-needs-tag">
                Pick one head to filter by outcome &mdash; an outcome is one head&rsquo;s verdict, so
                &ldquo;all heads&rdquo; has none.
              </span>
            )}
          </div>

          {imagesQ.isLoading ? (
            <div className="py-10 flex justify-center"><Spinner /></div>
          ) : imagesQ.error ? (
            <div className="mt-3"><ErrorBanner message={(imagesQ.error as Error).message} /></div>
          ) : images.length === 0 ? (
            <p className="mt-8 text-center text-sm text-[var(--color-ink-2)]" data-testid="no-images">
              No photographs match this. Widen the outcome, change the head, or switch split.
            </p>
          ) : (
            <>
              <ul
                className="mt-3 grid gap-2"
                style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(15rem, 1fr))' }}
                data-testid="photo-grid"
              >
                {images.map((img, i) => (
                  <li
                    key={img.image_id}
                    data-testid={`photo-${img.image_id}`}
                    className="flex flex-col gap-1 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] p-1.5"
                  >
                    <button
                      type="button"
                      onClick={() => setLightbox({ images: galleryA, index: i })}
                      aria-label={`Open photo ${img.image_id}`}
                      className="block h-32 w-full rounded-[var(--radius-xs)] bg-[var(--color-inset)]"
                    >
                      <img
                        src={imageSrc({ sreality_url: '', storage_path: img.storage_path })}
                        alt={`Photo ${img.image_id}`}
                        loading="lazy"
                        className="h-full w-full rounded-[var(--radius-xs)] object-contain"
                      />
                    </button>

                    {/* One row per arm x mode, one column per head, in one order —
                      * so the eye travels DOWN a head and compares arms, which is
                      * the whole point of putting them on the same photograph. */}
                    <div
                      className="grid items-center gap-x-1 gap-y-0.5"
                      style={{ gridTemplateColumns: `minmax(4.5rem, auto) repeat(${Math.max(1, tileHeads.length)}, minmax(0, 1fr))` }}
                    >
                      <span />
                      {tileHeads.map((h) => (
                        <span
                          key={h.id}
                          title={h.label}
                          className="truncate text-[0.55rem] uppercase tracking-[0.08em] text-[var(--color-ink-4)]"
                        >
                          {shortHead(h.label)}
                        </span>
                      ))}
                      {armIds.flatMap((armId) => modeSel.map((mode) => (
                        <ArmRow
                          key={`${armId}|${mode}`}
                          label={armName(armId)}
                          mode={mode}
                          heads={tileHeads}
                          scores={img.scores.filter((s) => s.arm_id === armId && s.mode === mode)}
                        />
                      )))}
                    </div>
                    <span className="font-mono text-[0.6rem] tabular-nums text-[var(--color-ink-4)]">
                      #{img.image_id}{img.listing_id != null ? ` · listing ${img.listing_id}` : ''}
                    </span>
                  </li>
                ))}
              </ul>

              <div className="mt-4 flex items-center justify-center gap-3 text-xs">
                <button
                  type="button"
                  data-testid="page-prev"
                  disabled={cursors.length === 0}
                  onClick={() => {
                    const stack = [...cursors];
                    stack.pop();
                    setCursors(stack);
                    patch({ after: stack.length ? String(stack[stack.length - 1]) : null });
                  }}
                  className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-3 py-1 text-[var(--color-ink-3)] disabled:opacity-40"
                >
                  &larr; previous
                </button>
                <span className="tabular-nums text-[var(--color-ink-4)]">{images.length} photos</span>
                <button
                  type="button"
                  data-testid="page-next"
                  disabled={nextAfter == null}
                  onClick={() => {
                    if (nextAfter == null) return;
                    setCursors([...cursors, nextAfter]);
                    patch({ after: String(nextAfter) });
                  }}
                  className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-3 py-1 text-[var(--color-ink-3)] disabled:opacity-40"
                >
                  next &rarr;
                </button>
              </div>
            </>
          )}
        </section>
      )}

      {/* ------------------------------------------------------------ view B */}
      {view === 'buckets' && (
        <section className="mt-4">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <span className="flex items-center gap-1">
              <Caption>arm</Caption>
              <select
                aria-label="Arm to inspect"
                data-testid="bucket-arm-picker"
                className={selectClass}
                value={bArmId ?? ''}
                onChange={(e) => patch({ barm: e.target.value, off: null })}
              >
                {arms.map((a) => <option key={a.id} value={a.id}>{a.arm}</option>)}
              </select>
            </span>
            <span className="flex flex-wrap items-center gap-1" role="group" aria-label="Training mode to inspect">
              <Caption>trained on</Caption>
              {MODES.map((m) => (
                <Chip
                  key={m}
                  testId={`bmode-${m}`}
                  on={bMode === m}
                  onClick={() => patch({ bmode: m, off: null })}
                  title={MODE_HELP[m]}
                >
                  {MODE_LABEL[m]}
                </Chip>
              ))}
            </span>
            <span className="text-[0.7rem] text-[var(--color-ink-3)]" data-testid="bucket-scope">
              One cell at a time: <b>{headName(tagId)}</b> on <b>{armName(bArmId)}</b>, trained{' '}
              <b>{MODE_LABEL[bMode]}</b>, on the <b>{SPLIT_LABEL[split].toLowerCase()}</b> numbers.
              Your arm and mode selections above are left alone.
            </span>
          </div>

          {tagId == null ? (
            <p className="mt-8 text-center text-sm text-[var(--color-ink-2)]">Pick a head.</p>
          ) : bucketsQ.isLoading ? (
            <div className="py-10 flex justify-center"><Spinner /></div>
          ) : bucketsQ.error ? (
            <div className="mt-3"><ErrorBanner message={(bucketsQ.error as Error).message} /></div>
          ) : !buckets ? null : (
            <>
              <div className="mt-3 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] p-3">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <h3 className="text-sm font-medium text-[var(--color-ink)]">Where the scores fell</h3>
                  <span className="text-[0.7rem] text-[var(--color-ink-3)]">
                    {split === 'exam' && (
                      <span data-testid="abstained-count">
                        {fmtN(buckets.abstained_count)} abstained
                        <span title={ABSTAINED_HELP} className="cursor-help"> (in no bucket)</span>
                        {' · '}
                      </span>
                    )}
                    scores are a {scoreUnit(bMode)}
                  </span>
                </div>
                <div className="mt-2">
                  <ScoreHistogram
                    hist={buckets.histogram}
                    threshold={bMetric?.threshold ?? null}
                    mode={bMode}
                  />
                </div>
              </div>

              <div
                className="mt-3 grid gap-3"
                style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(15rem, 1fr))' }}
              >
                {OUTCOMES.map((o) => {
                  const b = buckets.buckets[o];
                  const tiles = b?.tiles ?? [];
                  const gallery = tiles.map(asImagePublic);
                  return (
                    <div
                      key={o}
                      data-testid={`bucket-${o}`}
                      className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] p-2"
                    >
                      <div className="flex items-center gap-1.5" title={OUTCOME_HELP[o]}>
                        <ConfusionSquare tp={1} fp={1} fn={1} tn={1} only={o} size={16} />
                        <span className="text-sm text-[var(--color-ink)]">{OUTCOME_WORD[o]}</span>
                        <span className="text-[0.6rem] uppercase text-[var(--color-ink-4)]">{o}</span>
                        <span
                          className="ml-auto font-mono text-sm tabular-nums"
                          style={{ color: OUTCOME_COLOR[o] }}
                          data-testid={`bucket-${o}-count`}
                        >
                          {fmtN(b?.count ?? 0)}
                        </span>
                      </div>
                      <p className="mt-0.5 text-[0.6rem] text-[var(--color-ink-3)]">{OUTCOME_HELP[o]}</p>
                      {tiles.length === 0 ? (
                        <p className="mt-2 text-[0.7rem] text-[var(--color-ink-4)]">nothing on this page</p>
                      ) : (
                        <ul className="mt-2 grid grid-cols-3 gap-1">
                          {tiles.map((t, i) => (
                            <li key={t.image_id}>
                              <button
                                type="button"
                                onClick={() => setLightbox({ images: gallery, index: i })}
                                title={`#${t.image_id} · score ${t.score.toFixed(4)} · you said ${t.label === 1 ? 'yes' : t.label === 0 ? 'no' : 'nothing'}${t.fold != null ? ` · fold ${t.fold}` : ''}`}
                                aria-label={`Open photo ${t.image_id}`}
                                className="block w-full"
                              >
                                <img
                                  src={imageSrc({ sreality_url: '', storage_path: t.storage_path })}
                                  alt={`Photo ${t.image_id}`}
                                  loading="lazy"
                                  className="h-14 w-full rounded-[var(--radius-xs)] bg-[var(--color-inset)] object-cover"
                                />
                                <span className="block font-mono text-[0.55rem] tabular-nums text-[var(--color-ink-4)]">
                                  {t.score.toFixed(3)}
                                </span>
                              </button>
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  );
                })}
              </div>

              <div className="mt-4 flex flex-wrap items-center justify-center gap-3 text-xs">
                <button
                  type="button"
                  data-testid="bucket-prev"
                  disabled={offset === 0}
                  onClick={() => patch({ off: offset - PAGE_B > 0 ? String(offset - PAGE_B) : null })}
                  className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-3 py-1 text-[var(--color-ink-3)] disabled:opacity-40"
                >
                  &larr; previous
                </button>
                <span className="tabular-nums text-[var(--color-ink-4)]" data-testid="bucket-range">
                  most confident first &middot; {offset + 1}&ndash;{offset + PAGE_B} of each column
                </span>
                <button
                  type="button"
                  data-testid="bucket-next"
                  disabled={!OUTCOMES.some((o) => (buckets.buckets[o]?.count ?? 0) > offset + PAGE_B)}
                  onClick={() => patch({ off: String(offset + PAGE_B) })}
                  className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-3 py-1 text-[var(--color-ink-3)] disabled:opacity-40"
                >
                  next &rarr;
                </button>
              </div>
            </>
          )}
        </section>
      )}

      {/* ---------------------------------------------------------- the words */}
      <details className="mt-6 rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-3 py-2 text-xs text-[var(--color-ink-2)]">
        <summary className="cursor-pointer select-none text-[var(--color-ink)]">
          What every word on this page means
        </summary>
        <div className="mt-2 grid gap-4 md:grid-cols-2">
          <div>
            <p className="font-medium text-[var(--color-ink)]">The experiment</p>
            <ul className="mt-0.5 list-disc space-y-0.5 pl-4">
              <li><b>Arm</b> &mdash; one encoder configuration: a model at a revision, at a
                resolution, pooled and preprocessed a particular way. Hover an arm chip for its
                full identity.</li>
              <li><b>Head</b> &mdash; one yes/no classifier for one tag. One head per tag.</li>
              <li><b>Mode</b> &mdash; what the head was allowed to train on. Every mode is graded on
                the same photos, so &ldquo;this mode did better&rdquo; can never just mean it was
                asked an easier question.</li>
              <li><b>Cross-validation</b> &mdash; {SPLIT_HELP.cv}</li>
              <li><b>Exam</b> &mdash; {SPLIT_HELP.exam}</li>
            </ul>
            <p className="mt-2 font-medium text-[var(--color-ink)]">The three modes</p>
            <ul className="mt-0.5 list-disc space-y-0.5 pl-4">
              {MODES.map((m) => (
                <li key={m}><b>{MODE_LABEL[m]}</b> &mdash; {MODE_HELP[m]}</li>
              ))}
            </ul>
          </div>
          <div>
            <p className="font-medium text-[var(--color-ink)]">The numbers</p>
            <ul className="mt-0.5 list-disc space-y-0.5 pl-4">
              <li><b>Precision</b> &mdash; when it fires, how often it is right.</li>
              <li><b>Recall</b> &mdash; of the photos you said yes to, how many it caught.</li>
              <li><b>F1</b> &mdash; one number balancing the two. It is the column to scan.</li>
              <li><b>Graded n</b> &mdash; how many photos the number was computed over. A rate over
                nine photos is not the same claim as a rate over twelve hundred, which is why no
                rate on this page is ever shown without it.</li>
              <li><b>&mdash; / nothing proposed</b> &mdash; the head never fired, so there is no
                rate. Not zero.</li>
              <li><b>Abstained</b> &mdash; {ABSTAINED_HELP}</li>
              <li><b>Threshold</b> &mdash; the score at or above which the head says yes. Marked on
                the histogram.</li>
            </ul>
            <p className="mt-2 font-medium text-[var(--color-ink)]">The four outcomes</p>
            <ul className="mt-0.5 list-disc space-y-0.5 pl-4">
              {OUTCOMES.map((o) => (
                <li key={o}>
                  <b style={{ color: OUTCOME_COLOR[o] }}>{OUTCOME_WORD[o]}</b>{' '}
                  <span className="text-[0.6rem] uppercase text-[var(--color-ink-4)]">{o}</span>
                  {' — '}{OUTCOME_HELP[o]}
                </li>
              ))}
            </ul>
            <p className="mt-2">
              Scores compare only within one mode: the first two score between 0 and 1, closeness
              scores a cosine between &minus;1 and 1. Compare outcomes and the table, never the raw
              numbers across modes.
            </p>
          </div>
        </div>
      </details>

      {lightbox && lightbox.images.length > 0 && (
        <ImageLightbox
          images={lightbox.images}
          startIndex={lightbox.index}
          onClose={() => setLightbox(null)}
        />
      )}
    </div>
  );
}

/* One arm x mode line inside a photo tile: the arm's name, then one cell per head
 * in the SAME order for every arm, so the columns line up down the tile. */
function ArmRow({
  label, mode, heads, scores,
}: {
  label: string;
  mode: BakeoffMode;
  heads: ReadonlyArray<{ id: number; label: string }>;
  scores: BakeoffImageScore[];
}) {
  const byTag = new Map(scores.map((s) => [s.tag_id, s]));
  return (
    <>
      <span
        className="truncate text-[0.6rem] text-[var(--color-ink-2)]"
        title={`${label} · ${MODE_HELP[mode]}`}
      >
        {label}
        <span className="block truncate text-[0.52rem] text-[var(--color-ink-4)]">
          {MODE_LABEL[mode]}
        </span>
      </span>
      {heads.map((h) => {
        const s = byTag.get(h.id);
        if (!s) {
          return (
            <span
              key={h.id}
              className="text-center text-[0.6rem] text-[var(--color-ink-4)]"
              title="No score for this head on this arm."
            >
              —
            </span>
          );
        }
        const colour = s.outcome === 'abstained' ? 'var(--color-ink-3)' : OUTCOME_COLOR[s.outcome];
        const glyph = s.label === 1 ? '✓' : s.label === 0 ? '✗' : '–';
        const verdict = s.outcome === 'abstained'
          ? ABSTAINED_HELP
          : `${OUTCOME_WORD[s.outcome]} (${s.outcome}) — ${OUTCOME_HELP[s.outcome]}`;
        return (
          <span
            key={h.id}
            data-testid={`score-${s.arm_id}-${s.mode}-${s.tag_id}`}
            title={`${h.label} · ${label} · ${MODE_LABEL[s.mode]}\nscore ${s.score.toFixed(4)} (${scoreUnit(s.mode)}) · model said ${s.predicted ? 'yes' : 'no'} · you said ${s.label === 1 ? 'yes' : s.label === 0 ? 'no' : 'nothing'}\n${verdict}${s.fold != null ? `\nfold ${s.fold}` : ''}`}
            className="flex cursor-help items-center gap-1"
          >
            <span className="w-2 text-[0.6rem]" style={{ color: colour }}>{glyph}</span>
            <span className="relative h-1.5 flex-1 overflow-hidden rounded-[1px] bg-[var(--color-inset)]">
              <span
                className="absolute inset-y-0 left-0"
                style={{
                  width: `${barFraction(s.score, s.mode) * 100}%`,
                  background: colour,
                  opacity: s.outcome === 'abstained' ? 0.4 : 0.75,
                }}
              />
            </span>
          </span>
        );
      })}
    </>
  );
}
