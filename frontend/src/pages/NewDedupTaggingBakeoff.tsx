import { useMemo, useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';

import {
  getBakeoffBuckets,
  getBakeoffImageDetail,
  getBakeoffImages,
  getBakeoffMetrics,
  getBakeoffScores,
  listBakeoffRuns,
  type BakeoffArm,
  type BakeoffImageDetailScore,
  type BakeoffImageScore,
  type BakeoffMetric,
  type BakeoffMode,
  type BakeoffOutcome,
  type BakeoffSplit,
} from '@/lib/api';
import ErrorBanner from '@/components/ErrorBanner';
import ImageLightbox from '@/components/ImageLightbox';
import ImageSizeToggle from '@/components/ImageSizeToggle';
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
 *   4. VIEW C     — the same cell UNBUCKETED: every photo it scored, ranked.
 * The operator moves number → photo → mistake without losing their selection:
 * arms, modes, head and split live in the URL and survive the view switch.
 *
 * VIEW C IS RANKED BY THE HEAD'S SCORE, NOT BY F1. The operator asked for the
 * list "sorted by the F1 score", and F1 is a property of a HEAD — one number per
 * (arm, mode, head) — so it cannot order photographs. The per-photo sort key is
 * the score that head gave that photo, which is what F1 is computed FROM. The
 * view says so in its own help line rather than quietly substituting.
 *
 * THE NARROWING (operator ruling 2026-09-09 b). Two training modes and every arm
 * below 512 px are RETIRED: hidden from the selectors and therefore from the
 * matrix, never deleted. One toggle brings them back, and anything the URL names
 * stays visible regardless — a shared link must reproduce its screen, and a
 * selection you cannot see is a selection you cannot unpick. THE EXPERIMENT LANE
 * WAS NARROWED TO MATCH, in the same PR and by the same numbers: the trainer's
 * default modes (scripts/tag_head_bakeoff.LIVE_MODES) and the manifest's default
 * arms (scripts/tagging_bakeoff_arms.live_arms, same 504 floor). Hiding a set-up
 * here while the lane kept re-running it would cost GPU money invisibly.
 *
 * THE MODAL (operator ruling 2026-09-09 a). Tags will be assigned WINNER-TAKES-
 * ALL: every head scores the photo and the strongest wins. So opening a photo in
 * ANY view shows, beside it, each head's raw probability sorted strongest-first,
 * with the top one marked — the model's own output, never an F1. Heads are ranked
 * only WITHIN one (arm, mode, split); the scales differ across modes and two arms
 * are two different models.
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
 *   * /scores pages by offset over a total order, so View C carries the
 *     training-set grid's controls unchanged: the same page sizes, "x–y of N",
 *     a last-page jump, and the Small/Large photo switch.
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

/* RETIRED BY THE OPERATOR, 2026-09-09: the two positive-only modes lost the
 * bake-off and stop competing. Their rows are still in the database and still
 * render behind the toggle — this narrows the experiment, it does not erase a
 * result. */
const RETIRED_MODES: readonly BakeoffMode[] = ['pos_only_free_neg', 'pos_only_centroid'];
const LIVE_MODES: readonly BakeoffMode[] = MODES.filter((m) => !RETIRED_MODES.includes(m));

/* Retired the same day and for the same reason: an arm below 512 px is out.
 * The floor is written as 504 because dinov2 tokenises in 14 px patches, so ITS
 * 512 lands on 504 — the same arm snapped to its own grid, not a smaller one.
 * An arm whose resolution is unknown is left visible: hiding a thing we cannot
 * measure is how a live arm disappears without anyone deciding it should. */
const MIN_LIVE_RESOLUTION = 504;
const armIsRetired = (a: BakeoffArm): boolean =>
  a.resolution != null && a.resolution < MIN_LIVE_RESOLUTION;

const RETIRED_HELP =
  'Set-ups the operator retired on 2026-09-09: the two positive-only training modes, and every arm below 512 px (dinov2’s 504 is that same 512 snapped to its 14 px patch grid, so it stays). Among them is clip-b32-stored, the free copy of the incumbent’s live vectors that every other arm was measured against — reveal it when you want the old baseline back in the comparison. Nothing was deleted, here or in the database, and the bake-off lane now defaults to the same live set, so no new run pays to re-measure these.';

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
/* View C's page sizes are the training-set grid's, to the number: the operator
 * reads a ranking the way they read a tray, widening the page until the whole
 * cell is one scroll. */
const SCORE_PAGE_SIZES = [50, 100, 500, 2000, 10000] as const;
type ScorePageSize = (typeof SCORE_PAGE_SIZES)[number];
const DEFAULT_SCORE_PAGE: ScorePageSize = 50;

export default function NewDedupTaggingBakeoff() {
  const [params, setParams] = useSearchParams();
  const [lightbox, setLightbox] = useState<{ images: ImagePublic[]; index: number } | null>(null);
  /* Forward-only cursor paging (the contract gives `next_after_image_id` and no
   * inverse), so "previous" is a stack this page keeps. */
  const [cursors, setCursors] = useState<number[]>([]);
  /* View C's photo size — the same switch, and the same two sizes, as the
   * training-set grid, so "large" means one thing on both pages. */
  const [large, setLarge] = useState(false);

  const patch = (next: Record<string, string | null>) => {
    const merged = new URLSearchParams(params);
    for (const [k, v] of Object.entries(next)) {
      if (v === null || v === '') merged.delete(k);
      else merged.set(k, v);
    }
    setParams(merged, { replace: true });
  };

  /* A cursor is a position in ONE ordering. Change which rows are being listed
   * — the run, the cell, the split, the head — and every stored position is
   * about a list that no longer exists, so all three are dropped together. */
  const REWIND = { after: null, off: null, soff: null };
  const rewind = () => { setCursors([]); };

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
   * a shared link then says only what the operator actually chose. The DEFAULTS
   * are the narrowed experiment: every live arm, the full training mode, the
   * first head, cross-validation. */
  const armIds = useMemo(() => {
    const raw = (params.get('arms') ?? '').split(',').map(Number)
      .filter((n) => Number.isFinite(n) && n > 0);
    const known = raw.filter((id) => arms.some((a) => a.id === id));
    if (known.length) return known;
    const live = arms.filter((a) => !armIsRetired(a));
    /* Falling back to every arm when none survives the floor: a page with no
     * columns would read as "the run has no arms", which is a different claim. */
    return (live.length ? live : arms).map((a) => a.id);
  }, [params, arms]);

  const modeSel = useMemo(() => {
    const raw = (params.get('mode') ?? '').split(',')
      .filter((m): m is BakeoffMode => (MODES as readonly string[]).includes(m));
    return raw.length ? raw : [...LIVE_MODES];
  }, [params]);

  const split: BakeoffSplit = params.get('split') === 'exam' ? 'exam' : 'cv';
  const rawView = params.get('view');
  const view = rawView === 'buckets' || rawView === 'scores' ? rawView : 'photos';
  const showRetired = params.get('retired') === '1';

  /* 'all' is a real choice in View A, and the reason the outcome filter can be
   * unavailable — it is not the absence of a choice. View B has no such option:
   * a bucket is one head's, so 'all' there resolves to the first head. */
  const rawTag = params.get('tag');
  const tagId = rawTag === 'all' && view === 'photos'
    ? null
    : (Number(rawTag ?? 0) || heads[0]?.id || null);

  /* Turning an arm or mode off at the top also lets go of it below: the cell
   * pickers (Views B and C, and the zoom panel) offer only what is turned on
   * here, so a cell left pointing at a switched-off arm would be a choice with
   * no control to undo it. The cell then falls back to the first selected. */
  const toggleArm = (id: number) => {
    const next = armIds.includes(id) ? armIds.filter((a) => a !== id) : [...armIds, id];
    const kept = next.length ? next : [id];
    rewind();
    patch({
      arms: kept.join(','), ...REWIND,
      ...(bArmId != null && !kept.includes(bArmId) ? { barm: null } : {}),
    });
  };
  const toggleMode = (m: BakeoffMode) => {
    const next = modeSel.includes(m) ? modeSel.filter((x) => x !== m) : [...modeSel, m];
    const kept = next.length ? next : [m];
    rewind();
    patch({
      mode: kept.join(','), ...REWIND,
      ...(kept.includes(bMode) ? {} : { bmode: null }),
    });
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

  /* ------------------------------------------------------------- view C data */

  /* View C looks at the SAME cell as View B and shares its arm/mode keys, so
   * switching between "what did it get wrong" and "the whole ranking" never
   * re-asks which cell. It pages by offset, exactly as the training-set grid
   * does (`n` = page size, `soff` = offset): the API orders by score with
   * image_id as the unique tiebreaker, so an offset is a stable position even
   * though scores tie, and a link reproduces the page it was copied from. */
  const rawScoreN = Number(params.get('n') ?? DEFAULT_SCORE_PAGE);
  const scorePageSize: ScorePageSize = (SCORE_PAGE_SIZES as readonly number[]).includes(rawScoreN)
    ? (rawScoreN as ScorePageSize) : DEFAULT_SCORE_PAGE;
  const scoreOffset = Math.max(0, Number(params.get('soff') ?? 0) || 0);

  const scoresQ = useQuery({
    queryKey: ['bakeoff-scores', runId, bArmId, bMode, tagId, split, scoreOffset, scorePageSize],
    queryFn: () => getBakeoffScores(runId as number, {
      arm_id: bArmId as number, mode: bMode, tag_id: tagId as number, split,
      limit: scorePageSize, offset: scoreOffset,
    }),
    enabled: runId != null && view === 'scores' && bArmId != null && tagId != null,
  });
  const scoreRows = useMemo(() => scoresQ.data?.data.rows ?? [], [scoresQ.data]);
  const galleryC = useMemo(() => scoreRows.map(asImagePublic), [scoreRows]);
  const scoreTotal = scoresQ.data?.data.total ?? 0;
  /* Rank is a position in the whole ranking, not on this page — the operator is
   * looking for "how far down does the good stuff go", and a counter restarting
   * at 1 on every page would answer a different question. With offset paging
   * the position is always known: it is the offset. */
  const rankBase = scoreOffset;
  /* The last page's offset, from the cell's total — the same arithmetic as the
   * training-set grid's "last page" jump. */
  const lastScoreOffset = Math.max(
    0, Math.floor(Math.max(0, scoreTotal - 1) / scorePageSize) * scorePageSize,
  );

  /* --------------------------------------------------- what stays on screen */

  /* A retired arm or mode is hidden — UNLESS something on screen is using it.
   * The URL may name one (a link from before the ruling, or a deliberate
   * comparison), and a selection with no control is a selection the operator
   * cannot undo. */
  const visibleArms = useMemo(() => {
    if (showRetired) return arms;
    const inUse = new Set([...armIds, ...(bArmId != null ? [bArmId] : [])]);
    return arms.filter((a) => !armIsRetired(a) || inUse.has(a.id));
  }, [arms, armIds, bArmId, showRetired]);

  const visibleModes = useMemo(() => {
    if (showRetired) return MODES;
    return MODES.filter(
      (m) => !RETIRED_MODES.includes(m) || modeSel.includes(m) || m === bMode);
  }, [modeSel, bMode, showRetired]);

  const hiddenCount = (arms.length - visibleArms.length) + (MODES.length - visibleModes.length);

  /* WHAT THE CELL PICKERS OFFER — operator ruling 2026-09-09: the arm and mode
   * chips at the top are THE selection, and every picker below answers within
   * it. The one exception is an arm or mode a link already names for the cell
   * (`barm` / `bmode` outside the selection): it stays listed so it can be
   * undone, exactly as a retired arm in use stays visible above. */
  const cellArms = useMemo(
    () => arms.filter((a) => armIds.includes(a.id) || a.id === bArmId),
    [arms, armIds, bArmId],
  );
  const cellModes = useMemo(
    () => MODES.filter((m) => modeSel.includes(m) || m === bMode),
    [modeSel, bMode],
  );

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

  /* Views B and C look at the SAME one cell from two angles — the four outcome
   * columns, and the whole ranking — so they share one set of controls under
   * their own URL keys. Written once: two copies would drift, and the operator
   * would have to re-pick the cell every time they switched angle. */
  const cellControls = (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
      <span className="flex items-center gap-1">
        <Caption>arm</Caption>
        <select
          aria-label="Arm to inspect"
          data-testid="bucket-arm-picker"
          className={selectClass}
          value={bArmId ?? ''}
          onChange={(e) => { rewind(); patch({ barm: e.target.value, ...REWIND }); }}
        >
          {cellArms.map((a) => <option key={a.id} value={a.id}>{a.arm}</option>)}
        </select>
      </span>
      <span className="flex flex-wrap items-center gap-1" role="group" aria-label="Training mode to inspect">
        <Caption>trained on</Caption>
        {cellModes.map((m) => (
          <Chip
            key={m}
            testId={`bmode-${m}`}
            on={bMode === m}
            onClick={() => { rewind(); patch({ bmode: m, ...REWIND }); }}
            title={MODE_HELP[m]}
          >
            {MODE_LABEL[m]}
          </Chip>
        ))}
      </span>
      <span className="text-[0.7rem] text-[var(--color-ink-3)]" data-testid="bucket-scope">
        One cell at a time: <b>{headName(tagId)}</b> on <b>{armName(bArmId)}</b>, trained{' '}
        <b>{MODE_LABEL[bMode]}</b>, on the <b>{SPLIT_LABEL[split].toLowerCase()}</b> numbers.
        These pickers offer only the arms and modes turned on above; the selection there is
        left alone.
      </span>
    </div>
  );

  /* Every view opens the SAME modal, so the probabilities read the same wherever
   * the operator got to the photograph from. */
  const asideForImage = (image: ImagePublic | undefined) => (
    image && runId != null
      ? (
        <ImageProbabilityPanel
          runId={runId}
          imageId={image.id}
          split={split}
          armIds={armIds}
          modes={modeSel}
        />
      )
      : null
  );

  return (
    <div className="max-w-[112rem] mx-auto px-4 py-6">
      {/* ------------------------------------------------------------ header */}
      <header className="border-b border-[var(--color-rule)] pb-3">
        <h1 className="text-lg font-medium text-[var(--color-ink)]">Tagging bake-off</h1>
        <p className="mt-0.5 max-w-prose text-xs text-[var(--color-ink-3)]">
          One yes/no classifier &mdash; a <b>head</b> &mdash; per photo tag, trained on the
          picture-numbers of each encoder configuration &mdash; an <b>arm</b> &mdash; under each
          training <b>mode</b>, then asked about every photo you have labelled. The table says
          which arm did best on which tag, and three views sit under it:
          {' '}<b>Photos</b> puts one photograph in front of you with what every selected arm said
          about it; <b>By head</b> sorts one head&rsquo;s photos into what it caught, wrongly
          caught, missed and correctly rejected; <b>All photos by score</b> is that same head
          without the four columns &mdash; every photo it scored, strongest first. Open any photo
          in any view to see each head&rsquo;s probability for it, strongest first.
        </p>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Caption>run</Caption>
          <select
            aria-label="Bake-off run"
            data-testid="run-picker"
            className={selectClass}
            value={runId ?? ''}
            onChange={(e) => { rewind(); patch({ run: e.target.value, ...REWIND }); }}
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
            {visibleArms.map((a) => (
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
            {visibleModes.map((m) => (
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

          <span className="flex flex-wrap items-center gap-1">
            <Chip
              testId="toggle-retired"
              on={showRetired}
              title={RETIRED_HELP}
              onClick={() => patch({ retired: showRetired ? null : '1' })}
            >
              {showRetired ? 'Hide retired set-ups' : 'Show retired set-ups'}
              {!showRetired && hiddenCount > 0 && (
                <span className="ml-1 text-[0.6rem] text-[var(--color-ink-4)]" data-testid="retired-count">
                  {hiddenCount}
                </span>
              )}
            </Chip>
          </span>

          <span className="flex flex-wrap items-center gap-1" role="group" aria-label="Which numbers">
            <Caption>numbers from</Caption>
            {SPLITS.map((s) => (
              <Chip
                key={s}
                testId={`split-${s}`}
                on={split === s}
                onClick={() => { rewind(); patch({ split: s, ...REWIND }); }}
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
        <Chip
          testId="view-scores"
          on={view === 'scores'}
          onClick={() => patch({ view: 'scores' })}
          title="The same head, arm and mode as the buckets — but every photo it scored in one ranked list instead of four columns."
        >
          All photos by score
        </Chip>

        <span className="ml-2 flex items-center gap-1">
          <Caption>head</Caption>
          <select
            aria-label="Head"
            data-testid="tag-picker"
            className={selectClass}
            value={tagId == null ? 'all' : String(tagId)}
            onChange={(e) => {
              rewind();
              patch({ tag: e.target.value, ...REWIND, outcome: null });
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
              onClick={() => { rewind(); patch({ outcome: null, ...REWIND }); }}
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
                onClick={() => { rewind(); patch({ outcome: o, ...REWIND }); }}
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
          {cellControls}

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

      {/* ------------------------------------------------------------ view C */}
      {view === 'scores' && (
        <section className="mt-4">
          {cellControls}
          <p className="mt-1.5 max-w-prose text-[0.7rem] text-[var(--color-ink-3)]" data-testid="scores-help">
            Every photo this head scored, most confident first &mdash; the four buckets of{' '}
            <b>By head</b> poured into one list, plus the photos you abstained on, which are in no
            bucket. The order is the <b>head&rsquo;s own score for each photo</b>, not F1: an F1 is
            one number for the whole head, so it can rank heads against each other but cannot rank
            photographs.
          </p>

          {tagId == null || bArmId == null ? (
            <p className="mt-8 text-center text-sm text-[var(--color-ink-2)]">Pick a head.</p>
          ) : scoresQ.isLoading ? (
            <div className="py-10 flex justify-center"><Spinner /></div>
          ) : scoresQ.error ? (
            <div className="mt-3"><ErrorBanner message={(scoresQ.error as Error).message} /></div>
          ) : scoreRows.length === 0 && scoreOffset === 0 ? (
            <p className="mt-8 text-center text-sm text-[var(--color-ink-2)]" data-testid="no-scores">
              This head scored no photographs on the {SPLIT_LABEL[split].toLowerCase()} split.
            </p>
          ) : (
            <>
              <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[0.7rem] text-[var(--color-ink-3)]">
                <span data-testid="scores-total">
                  {fmtN(scoreTotal)} photo{scoreTotal === 1 ? '' : 's'} in this cell
                </span>
                <span className="text-[var(--color-ink-4)]" data-testid="scores-range">
                  {scoreRows.length > 0 && (
                    <>showing {fmtN(rankBase + 1)}&ndash;{fmtN(rankBase + scoreRows.length)}{' · '}</>
                  )}
                  scores are a {scoreUnit(bMode)}
                </span>
                <span className="ml-auto">
                  <ImageSizeToggle large={large} onChange={setLarge} label="Ranking grid image size" />
                </span>
              </div>

              {scoreRows.length === 0 ? (
                /* A link can name an offset past the cell's end (the cell was
                 * re-scored smaller, or the page size changed). That is the end
                 * of the ranking, not an empty cell — and the pager below still
                 * renders, so this is never a dead end. */
                <p className="mt-6 text-center text-sm text-[var(--color-ink-2)]" data-testid="scores-end">
                  The ranking ends here. Step back for the previous page.
                </p>
              ) : (
              <ul
                className="mt-2 grid gap-2"
                style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${large ? '16rem' : '8rem'}, 1fr))` }}
                data-testid="score-grid"
                data-size={large ? 'large' : 'small'}
              >
                {scoreRows.map((r, i) => {
                  const colour = r.outcome === 'abstained'
                    ? 'var(--color-ink-3)'
                    : OUTCOME_COLOR[r.outcome];
                  const said = r.label === 1 ? 'yes' : r.label === 0 ? 'no' : 'nothing';
                  const verdict = r.outcome === 'abstained'
                    ? ABSTAINED_HELP
                    : `${OUTCOME_WORD[r.outcome]} (${r.outcome}) — ${OUTCOME_HELP[r.outcome]}`;
                  return (
                    <li
                      key={r.image_id}
                      data-testid={`score-row-${r.image_id}`}
                      className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] p-1.5"
                      /* The outcome colour is a LEFT EDGE, not a fill: the photo
                       * is the thing being judged and a tinted tile would change
                       * how it reads. */
                      style={{ borderLeft: `3px solid ${colour}` }}
                    >
                      <button
                        type="button"
                        onClick={() => setLightbox({ images: galleryC, index: i })}
                        aria-label={`Open photo ${r.image_id}`}
                        title={`#${r.image_id} · score ${r.score.toFixed(4)} (${scoreUnit(bMode)}) · model said ${r.predicted ? 'yes' : 'no'} · you said ${said}\n${verdict}${r.fold != null ? `\nfold ${r.fold}` : ''}`}
                        className={`block w-full rounded-[var(--radius-xs)] bg-[var(--color-inset)] ${large ? 'h-56' : 'h-28'}`}
                      >
                        <img
                          src={imageSrc({ sreality_url: '', storage_path: r.storage_path })}
                          alt={`Photo ${r.image_id}`}
                          loading="lazy"
                          className="h-full w-full rounded-[var(--radius-xs)] object-cover"
                        />
                      </button>
                      <div className="mt-1 flex flex-wrap items-baseline gap-x-1">
                        <span className="font-mono text-[0.55rem] tabular-nums text-[var(--color-ink-4)]">
                          {fmtN(rankBase + i + 1)}
                        </span>
                        <span
                          className="font-mono text-xs tabular-nums"
                          style={{ color: colour }}
                          data-testid={`score-row-${r.image_id}-score`}
                        >
                          {r.score.toFixed(3)}
                        </span>
                        <span
                          className="ml-auto text-[0.6rem] cursor-help"
                          style={{ color: colour }}
                          title={verdict}
                          data-testid={`score-row-${r.image_id}-label`}
                        >
                          you said {said}
                        </span>
                      </div>
                      <span className="relative mt-0.5 block h-1.5 overflow-hidden rounded-[1px] bg-[var(--color-inset)]">
                        <span
                          className="absolute inset-y-0 left-0"
                          style={{
                            width: `${barFraction(r.score, bMode) * 100}%`,
                            background: colour,
                            opacity: r.outcome === 'abstained' ? 0.4 : 0.75,
                          }}
                        />
                      </span>
                    </li>
                  );
                })}
              </ul>
              )}

              {/* The training-set grid's pager, control for control: page size,
                * previous, "x–y of N", next, and a jump to the last page. A
                * page-size change restarts at the top — an offset is a position
                * in pages of ONE size, so it does not carry across. */}
              <div className="mt-4 flex flex-wrap items-center justify-center gap-3 text-xs">
                <span className="flex items-center gap-1" role="group" aria-label="per page">
                  <span className="mr-0.5 text-[0.65rem] uppercase tracking-[0.1em] text-[var(--color-ink-4)]">per page</span>
                  {SCORE_PAGE_SIZES.map((n) => (
                    <button
                      key={n}
                      type="button"
                      data-testid={`score-per-page-${n}`}
                      aria-pressed={scorePageSize === n}
                      onClick={() => patch({ n: String(n), soff: null })}
                      className={`rounded-[var(--radius-sm)] border px-2 py-1 ${scorePageSize === n ? 'border-[var(--color-ink-2)] text-[var(--color-ink)]' : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink)]'}`}
                    >
                      {n}
                    </button>
                  ))}
                </span>
                <button
                  type="button"
                  data-testid="score-prev"
                  disabled={scoreOffset === 0}
                  onClick={() => {
                    const back = Math.max(0, scoreOffset - scorePageSize);
                    patch({ soff: back === 0 ? null : String(back) });
                  }}
                  className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-3 py-1 text-[var(--color-ink-3)] disabled:opacity-40"
                >
                  &larr; previous
                </button>
                <span className="tabular-nums text-[var(--color-ink-4)]" data-testid="score-page-range">
                  {scoreRows.length > 0 ? <>{fmtN(scoreOffset + 1)}&ndash;{fmtN(scoreOffset + scoreRows.length)}</> : 'none'}
                  {' of '}{fmtN(scoreTotal)}
                </span>
                <button
                  type="button"
                  data-testid="score-next"
                  disabled={scoreOffset + scoreRows.length >= scoreTotal}
                  onClick={() => patch({ soff: String(scoreOffset + scorePageSize) })}
                  className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-3 py-1 text-[var(--color-ink-3)] disabled:opacity-40"
                >
                  next &rarr;
                </button>
                {lastScoreOffset > scoreOffset && (
                  <button
                    type="button"
                    data-testid="score-last"
                    onClick={() => patch({ soff: String(lastScoreOffset) })}
                    className="rounded-[var(--radius-sm)] border border-[var(--color-sage)] px-3 py-1 text-[var(--color-ink)]"
                  >
                    last page &#8677;
                  </button>
                )}
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
              <li><b>Retired</b> &mdash; {RETIRED_HELP}</li>
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
              <li><b>Score</b> &mdash; what the head actually output for one photograph, before any
                yes/no line is drawn. It is what ranks the photos in <b>All photos by score</b>,
                and it is what F1 is computed from &mdash; F1 itself is one number for a whole
                head, so it can rank heads but never photographs.</li>
              <li><b>Winner</b> &mdash; in the photo modal, the head with the highest score for
                that photograph on one arm, mode and split. That is how a tag will be assigned:
                every head scores the photo and the strongest wins, so adding a head later can
                change the answer without anything being re-decided.</li>
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
          /* Indexed by POSITION, not captured at open: the arrow keys walk the
           * gallery inside the viewer, and the panel must follow the photo. */
          asideAt={(i) => asideForImage(lightbox.images[i])}
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


/* THE PER-IMAGE PROBABILITY PANEL — the modal's right-hand column, and the one
 * place on this page that shows RAW MODEL OUTPUT rather than a measurement.
 *
 * Tags will be assigned WINNER-TAKES-ALL (operator ruling 2026-09-09 a): every
 * head scores the photo, and the strongest score names the tag. No per-head
 * yes/no decision is taken in the product at all — which is why this panel ranks
 * heads by score and marks the top one, and why no F1 appears in it. Heads will
 * be ADDED over time, so the winner is recomputed here from whatever heads the
 * run scored; it is never a stored verdict.
 *
 * ONE RANKING PER (arm, mode, split), NEVER ACROSS THEM. Two arms are two
 * different models, the logistic modes score in [0, 1] while the centroid mode
 * is a cosine, and the two splits are different questions. So each group is
 * ranked and won independently and the page never puts them in one list.
 *
 * The panel opens filtered to what the operator was already looking at — their
 * selected arms and modes, on the split on show — with one chip to widen to
 * every group the run has. */
function ImageProbabilityPanel({
  runId, imageId, split, armIds, modes,
}: {
  runId: number;
  imageId: number;
  split: BakeoffSplit;
  armIds: number[];
  modes: BakeoffMode[];
}) {
  const [wide, setWide] = useState(false);
  const q = useQuery({
    queryKey: ['bakeoff-image-detail', runId, imageId],
    queryFn: () => getBakeoffImageDetail(runId, imageId),
  });

  const detail = q.data?.data ?? null;

  const groups = useMemo(() => {
    const rows = detail?.scores ?? [];
    const kept = wide
      ? rows
      : rows.filter((s) => s.split === split && armIds.includes(s.arm_id)
                           && modes.includes(s.mode));
    const by = new Map<string, { arm: string; armId: number; mode: BakeoffMode;
                                 split: BakeoffSplit; rows: BakeoffImageDetailScore[] }>();
    for (const s of kept) {
      const key = `${s.arm_id}|${s.mode}|${s.split}`;
      const g = by.get(key)
        ?? { arm: s.arm, armId: s.arm_id, mode: s.mode, split: s.split, rows: [] };
      g.rows.push(s);
      by.set(key, g);
    }
    for (const g of by.values()) g.rows.sort((a, b) => b.score - a.score);
    return [...by.values()].sort(
      (a, b) => a.armId - b.armId || a.mode.localeCompare(b.mode)
                || a.split.localeCompare(b.split));
  }, [detail, wide, split, armIds, modes]);

  /* The one-line answer per model, read off the top of each ranking: the head
   * a winner-takes-all reading gives this photo under that arm, and how many
   * heads it beat. That count matters — on cross-validation a photo is scored
   * only by the heads whose training set holds it, so a "winner" among one
   * head was never a contest, and the line says so rather than dressing it up. */
  const manyModes = new Set(groups.map((g) => g.mode)).size > 1;

  return (
    <div data-testid="probability-panel" className="text-xs">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-medium text-[var(--color-ink)]">
          What each head made of this photo
        </h3>
        <Chip
          testId="probability-widen"
          on={wide}
          onClick={() => setWide(!wide)}
          title="Every arm, mode and split this run scored the photo under — not only the ones you have selected."
        >
          {wide ? 'Your selection' : 'Every arm'}
        </Chip>
      </div>
      <p className="mt-1 text-[0.68rem] text-[var(--color-ink-3)]">
        The raw number each head gave this photograph, strongest first &mdash; not an F1. The
        strongest head is the tag a winner-takes-all reading would assign, so it is marked{' '}
        <b>winner</b>. Heads are only ever ranked <b>within</b> one arm, mode and split: two arms
        are two different models, and the modes do not share a scale. The colour on each row is
        that head&rsquo;s own yes/no verdict at the run&rsquo;s threshold &mdash; a
        measurement, kept here because it says which rows the experiment scored right, and read by
        nothing that assigns a tag.
      </p>
      {detail?.listing_id != null && (
        <p className="mt-1 font-mono text-[0.6rem] tabular-nums text-[var(--color-ink-4)]">
          #{detail.image_id} &middot; listing {detail.listing_id}
        </p>
      )}

      {q.isLoading ? (
        <div className="py-6 flex justify-center"><Spinner /></div>
      ) : q.error ? (
        <div className="mt-2"><ErrorBanner message={(q.error as Error).message} /></div>
      ) : groups.length === 0 ? (
        <p className="mt-3 text-[var(--color-ink-2)]" data-testid="probability-empty">
          No head scored this photo under the arms, modes and split you have selected. Widen to{' '}
          <b>Every arm</b> to see what the run does hold for it.
        </p>
      ) : (
        <>
        <div
          data-testid="top-heads"
          className="mt-2 rounded-[var(--radius-sm)] border border-[var(--color-copper)] bg-[var(--color-paper-2)] p-2"
        >
          <div className="text-[0.62rem] uppercase tracking-[0.16em] text-[var(--color-ink-4)]">
            top head per model
          </div>
          <ul className="mt-1 flex flex-col gap-0.5">
            {groups.map((g) => {
              const top = g.rows[0];
              const contest = g.rows.length === 1
                ? 'only head that scored it'
                : `of ${g.rows.length} heads`;
              return (
                <li
                  key={`${g.armId}|${g.mode}|${g.split}`}
                  data-testid={`top-head-${g.armId}-${g.mode}-${g.split}`}
                  className="grid items-baseline gap-x-1.5"
                  style={{ gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr) auto auto' }}
                >
                  <span
                    className="truncate text-[var(--color-ink)]"
                    title={`${g.arm} · ${MODE_LABEL[g.mode]} · ${SPLIT_LABEL[g.split]}`}
                  >
                    {g.arm}
                    {manyModes && <span className="text-[var(--color-ink-3)]"> · {MODE_LABEL[g.mode]}</span>}
                    {wide && <span className="text-[var(--color-ink-4)]"> · {SPLIT_LABEL[g.split].toLowerCase()}</span>}
                  </span>
                  <span className="truncate text-[var(--color-ink-2)]" title={top.tag_label ?? undefined}>
                    {shortHead(top.tag_label ?? `tag ${top.tag_id}`)}
                  </span>
                  <span className="font-mono text-[0.65rem] tabular-nums text-[var(--color-ink)]">
                    {top.score.toFixed(3)}
                  </span>
                  <span
                    className="cursor-help text-[0.6rem] text-[var(--color-ink-4)]"
                    title={g.rows.length === 1
                      ? 'No other head scored this photo under this arm, mode and split, so there was nothing to beat. On cross-validation a photo is scored only by the heads whose training set holds it; the exam scores every photo with every head.'
                      : `The highest of ${g.rows.length} heads that scored this photo under this arm, mode and split.`}
                  >
                    {contest}
                  </span>
                </li>
              );
            })}
          </ul>
          <p className="mt-1 text-[0.62rem] text-[var(--color-ink-4)]">
            The strongest head under each model &mdash; the tag a winner-takes-all reading gives
            this photo. Models are listed only for the arms and modes turned on at the top of the
            page, on the split on show; <b>Every arm</b> widens it.
          </p>
        </div>
        <div className="mt-2 flex flex-col gap-3">
          {groups.map((g) => (
            <div
              key={`${g.armId}|${g.mode}|${g.split}`}
              data-testid={`probability-group-${g.armId}-${g.mode}-${g.split}`}
              className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] p-2"
            >
              <div className="flex flex-wrap items-baseline gap-x-1.5">
                <span className="text-[var(--color-ink)]">{g.arm}</span>
                <span className="text-[0.6rem] text-[var(--color-ink-3)]" title={MODE_HELP[g.mode]}>
                  {MODE_LABEL[g.mode]}
                </span>
                <span className="text-[0.6rem] text-[var(--color-ink-4)]">
                  {SPLIT_LABEL[g.split].toLowerCase()} &middot; {scoreUnit(g.mode)}
                </span>
              </div>
              <ul className="mt-1 flex flex-col gap-0.5">
                {g.rows.map((s, rank) => {
                  const colour = s.outcome === 'abstained'
                    ? 'var(--color-ink-3)'
                    : OUTCOME_COLOR[s.outcome];
                  const said = s.label === 1 ? 'yes' : s.label === 0 ? 'no' : 'nothing';
                  const glyph = s.label === 1 ? '✓' : s.label === 0 ? '✗' : '–';
                  return (
                    <li
                      key={s.tag_id}
                      data-testid={`probability-${g.armId}-${g.mode}-${g.split}-${s.tag_id}`}
                      className="grid items-center gap-x-1.5"
                      style={{ gridTemplateColumns: '1fr auto 5.4rem 0.8rem' }}
                    >
                      <span className="truncate text-[var(--color-ink-2)]" title={s.tag_label ?? undefined}>
                        {shortHead(s.tag_label ?? `tag ${s.tag_id}`)}
                      </span>
                      {rank === 0 ? (
                        <span
                          className="rounded-[2px] border border-[var(--color-copper)] px-1 text-[0.52rem] uppercase tracking-[0.08em] text-[var(--color-copper)]"
                          title="The strongest head on this arm, mode and split — the tag a winner-takes-all reading would give this photo."
                        >
                          winner
                        </span>
                      ) : <span />}
                      <span className="flex items-center gap-1">
                        {/* Normalised by its OWN mode's scale — the centroid
                          * mode is a cosine, and a bar drawn on the wrong axis
                          * is a lie about a number nobody can re-derive. */}
                        <span className="relative h-1.5 w-8 shrink-0 overflow-hidden rounded-[1px] bg-[var(--color-inset)]">
                          <span
                            className="absolute inset-y-0 left-0"
                            style={{
                              width: `${barFraction(s.score, s.mode) * 100}%`,
                              background: colour,
                              opacity: s.outcome === 'abstained' ? 0.4 : 0.75,
                            }}
                          />
                        </span>
                        <span
                          className="font-mono text-[0.65rem] tabular-nums"
                          style={{ color: colour }}
                        >
                          {s.score.toFixed(3)}
                        </span>
                      </span>
                      <span
                        className="cursor-help text-center text-[0.6rem]"
                        style={{ color: colour }}
                        data-testid={`probability-${g.armId}-${g.mode}-${g.split}-${s.tag_id}-label`}
                        title={`you said ${said}${s.outcome === 'abstained' ? ` — ${ABSTAINED_HELP}` : ` · ${OUTCOME_WORD[s.outcome]} (${s.outcome})`}`}
                      >
                        {glyph}
                      </span>
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </div>
        </>
      )}
    </div>
  );
}
