import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import { getDedupDecisionEvidence, type DedupEvidenceImage } from '@/lib/api';
import type { AuditRung } from '@/lib/types';
import { imageSrc } from '@/lib/imageUrl';
import { imageTagLabel } from '@/lib/imageTags';
import DedupBreakdown from '@/components/DedupBreakdown';

/* The evidence behind one dedup decision — the SAME component on Decision history
 * (merged / dismissed) and Needs-review (queued), so a row and a card read alike. Shows
 * the AUDITABLE breakdown (each signal vs its threshold, met / not met, with a deep-link
 * to the governing Settings knob) and a drawer that hydrates the SPECIFIC pictures the
 * decision turned on — the pHash matched pairs, the compared plans, or the deciding room.
 * Civic-archive: copper = match signal, brick = mismatch, borders-only depth. */

const REASON_LABELS: Record<string, string> = {
  address_exact: 'shoda adresy',
  image_phash: 'shodné fotky (pHash)',
  visual_match: 'vizuální shoda',
  visual_different: 'vizuální rozdíl',
  visual_inconclusive: 'vizuálně nejednoznačné',
  site_plan_different_unit: 'plán: jiná jednotka',
  floor_plan_different_layout: 'půdorys: jiná dispozice',
  floor_plan_review: 'půdorys: k ruční kontrole',
  no_images: 'bez fotek',
  vision_unavailable: 'vize nedostupná',
  auto_merge_off: 'auto-merge vypnut',
  manual: 'ruční',
  manual_cluster: 'ruční (shluk)',
  manual_subset: 'ruční (výběr)',
};

const VERDICT_STYLE: Record<string, string> = {
  High: 'text-[var(--color-copper)] border-[var(--color-copper)] bg-[var(--color-copper-soft)]',
  Medium: 'text-[var(--color-ink-3)] border-[var(--color-rule-strong)]',
  Low: 'text-[var(--color-brick)] border-[var(--color-brick)] bg-[var(--color-brick-soft)]',
};

const PILL =
  'inline-flex items-center gap-1 px-1.5 py-0.5 rounded-[var(--radius-xs)] border text-[0.68rem] tabular-nums';
const PILL_NEUTRAL = `${PILL} border-[var(--color-rule)] text-[var(--color-ink-3)]`;

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

export default function DedupFactors({
  factors,
  breakdown,
  leftSrealityId,
  rightSrealityId,
  categoryMain,
  batchPhotos,
}: {
  factors: Record<string, unknown> | null;
  // Server-computed auditable rungs. When present they REPLACE the ad-hoc numeric pills.
  breakdown?: AuditRung[];
  leftSrealityId: number | null;
  rightSrealityId: number | null;
  categoryMain?: string | null;
  // A broadcast command (open/close) from a parent "expand/collapse all" control. Only
  // re-applied when `seq` changes, so individual toggling still works between commands.
  batchPhotos?: { open: boolean; seq: number };
}) {
  const [showPhotos, setShowPhotos] = useState(false);
  const f = factors ?? {};

  const canShowPhotos = leftSrealityId != null && rightSrealityId != null;
  useEffect(() => {
    if (batchPhotos && canShowPhotos) setShowPhotos(batchPhotos.open);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batchPhotos?.seq]);

  const reason = typeof f.reason === 'string' ? f.reason : null;
  const stage = typeof f.stage === 'string' ? f.stage : null;
  const verdict = typeof f.verdict === 'string' ? f.verdict : null;
  const roomType = typeof f.room_type === 'string' ? f.room_type : null;
  const rationale = typeof f.rationale === 'string' ? f.rationale : null;
  const phashPairs = num(f.phash_pairs);
  const phashMin = num(f.phash_min_pairs);
  const cosine = num(f.cosine);
  const hasBreakdown = !!breakdown && breakdown.length > 0;

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-1.5">
        {reason && (
          <span className={PILL_NEUTRAL}>{REASON_LABELS[reason] ?? reason}</span>
        )}
        {roomType && (
          <span className={PILL_NEUTRAL} title="Porovnaná místnost">
            {imageTagLabel(roomType)}
          </span>
        )}
        {/* Legacy quick pills — only when the structured breakdown isn't available. */}
        {!hasBreakdown && verdict && (
          <span
            className={[PILL, VERDICT_STYLE[verdict] ?? PILL_NEUTRAL].join(' ')}
            title="Verdikt vizuálního porovnání (model)"
          >
            Vize: {verdict}
          </span>
        )}
        {!hasBreakdown && phashPairs != null && (
          <span
            className={[
              PILL,
              phashMin != null && phashPairs >= phashMin
                ? VERDICT_STYLE.High
                : PILL_NEUTRAL,
            ].join(' ')}
            title="Počet téměř shodných párů fotek vs. práh (Settings)"
          >
            pHash {phashPairs}
            {phashMin != null ? ` / ${phashMin}` : ''}
          </span>
        )}
        {!hasBreakdown && cosine != null && (
          <span className={PILL_NEUTRAL} title="CLIP kosinová podobnost místnosti">
            cos {cosine.toFixed(3)}
          </span>
        )}
        {canShowPhotos && (
          <button
            type="button"
            onClick={() => setShowPhotos((v) => !v)}
            className="text-[0.68rem] text-[var(--color-ink-4)] hover:text-[var(--color-copper)] underline decoration-dotted underline-offset-2"
          >
            {showPhotos ? 'Skrýt fotky' : 'Fotky'}
          </button>
        )}
      </div>

      {hasBreakdown && <DedupBreakdown rungs={breakdown} />}

      {rationale && (
        <p className="text-[0.74rem] leading-snug text-[var(--color-ink-3)] italic">
          {rationale}
        </p>
      )}

      {showPhotos && canShowPhotos && (
        <DecisionEvidence
          a={leftSrealityId as number}
          b={rightSrealityId as number}
          stage={stage}
          reason={reason}
          roomType={roomType}
          categoryMain={categoryMain ?? null}
        />
      )}
    </div>
  );
}

function DecisionEvidence({
  a,
  b,
  stage,
  reason,
  roomType,
  categoryMain,
}: {
  a: number;
  b: number;
  stage: string | null;
  reason: string | null;
  roomType: string | null;
  categoryMain: string | null;
}) {
  const q = useQuery({
    queryKey: ['dedup', 'decision-evidence', a, b, stage, reason, roomType, categoryMain],
    queryFn: () =>
      getDedupDecisionEvidence({
        a,
        b,
        stage,
        reason,
        room_type: roomType,
        category_main: categoryMain,
        per_side: 4,
      }),
    staleTime: 5 * 60_000,
  });
  if (q.isLoading) {
    return <p className="text-[0.72rem] text-[var(--color-ink-4)]">Načítám fotky…</p>;
  }
  const d = q.data?.data;
  if (!d) return null;
  const planRoom = d.room_type === 'floor_plan' || d.room_type === 'site_plan';
  return (
    <div className="flex flex-col gap-2">
      {d.pairs && d.pairs.length > 0 && (
        <div className="flex flex-col gap-1">
          <span className="text-[0.62rem] uppercase tracking-[0.1em] text-[var(--color-ink-4)]">
            Shodné páry fotek (pHash)
          </span>
          <div className="flex flex-wrap gap-2">
            {d.pairs.map((p, i) => (
              <div
                key={i}
                className="flex items-center gap-1 rounded-[var(--radius-xs)] border border-[var(--color-copper)]/40 bg-[var(--color-copper-soft)]/40 p-1"
                title={`Hammingova vzdálenost ${p.hamming}`}
              >
                <Thumb img={p.left} />
                <span className="text-[0.6rem] text-[var(--color-ink-4)] tabular-nums">
                  ⟷ {p.hamming}
                </span>
                <Thumb img={p.right} />
              </div>
            ))}
          </div>
        </div>
      )}
      {/* The compared plans / deciding room (skip the generic first-photo strips when the
          pHash pairs already ARE the evidence and there's no plan to show). */}
      {(planRoom || !d.pairs || d.pairs.length === 0) && (
        <div className="flex flex-col gap-1">
          {planRoom && (
            <span className="text-[0.62rem] uppercase tracking-[0.1em] text-[var(--color-ink-4)]">
              {d.room_type === 'floor_plan' ? 'Porovnané půdorysy' : 'Porovnané plány'}
            </span>
          )}
          <div className="grid grid-cols-2 gap-2">
            <PhotoStrip side={d.left} />
            <PhotoStrip side={d.right} />
          </div>
        </div>
      )}
    </div>
  );
}

function Thumb({ img }: { img: DedupEvidenceImage }) {
  return (
    <img
      src={imageSrc({ sreality_url: img.sreality_url ?? '', storage_path: img.storage_path })}
      alt=""
      loading="lazy"
      className="h-16 w-16 object-cover rounded-[var(--radius-xs)] border border-[var(--color-rule)] shrink-0"
    />
  );
}

function PhotoStrip({
  side,
}: {
  side: { sreality_id: number; images: DedupEvidenceImage[]; fallback: boolean };
}) {
  return (
    <div className="flex flex-col gap-1">
      <div className="flex gap-1 overflow-x-auto">
        {side.images.length === 0 ? (
          <span className="text-[0.7rem] text-[var(--color-ink-4)]">bez fotek</span>
        ) : (
          side.images.map((img, i) => <Thumb key={img.image_id ?? i} img={img} />)
        )}
      </div>
      {side.fallback && (
        <span className="text-[0.64rem] text-[var(--color-ink-4)]">
          náhradní výběr (místnost neklasifikována)
        </span>
      )}
    </div>
  );
}
