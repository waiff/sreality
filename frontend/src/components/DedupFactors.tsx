import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import { getDedupDecisionImages } from '@/lib/api';
import { imageSrc } from '@/lib/imageUrl';
import { imageTagLabel } from '@/lib/imageTags';

/* The evidence behind one dedup decision — the SAME component on Decision history
 * (merged / dismissed) and Needs-review (queued), so a row and a card read alike.
 * Shows the factors the operator tunes in Settings (pHash pairs vs threshold,
 * CLIP cosine, vision verdict) as compact pills, with a drawer that hydrates the
 * deciding room's photos for both listings. Civic-archive: copper = match signal,
 * brick = mismatch, borders-only depth, tabular numerals. */

const REASON_LABELS: Record<string, string> = {
  address_exact: 'shoda adresy',
  image_phash: 'shodné fotky (pHash)',
  visual_match: 'vizuální shoda',
  visual_different: 'vizuální rozdíl',
  visual_inconclusive: 'vizuálně nejednoznačné',
  site_plan_different_unit: 'plán: jiná jednotka',
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
  leftSrealityId,
  rightSrealityId,
  batchPhotos,
}: {
  factors: Record<string, unknown> | null;
  leftSrealityId: number | null;
  rightSrealityId: number | null;
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
  const verdict = typeof f.verdict === 'string' ? f.verdict : null;
  const roomType = typeof f.room_type === 'string' ? f.room_type : null;
  const rationale = typeof f.rationale === 'string' ? f.rationale : null;
  const phashPairs = num(f.phash_pairs);
  const phashMin = num(f.phash_min_pairs);
  const cosine = num(f.cosine);

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-1.5">
        {reason && (
          <span className={PILL_NEUTRAL}>{REASON_LABELS[reason] ?? reason}</span>
        )}
        {verdict && (
          <span
            className={[PILL, VERDICT_STYLE[verdict] ?? PILL_NEUTRAL].join(' ')}
            title="Verdikt vizuálního porovnání (model)"
          >
            Vize: {verdict}
          </span>
        )}
        {roomType && (
          <span className={PILL_NEUTRAL} title="Porovnaná místnost">
            {imageTagLabel(roomType)}
          </span>
        )}
        {phashPairs != null && (
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
        {cosine != null && (
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

      {rationale && (
        <p className="text-[0.74rem] leading-snug text-[var(--color-ink-3)] italic">
          {rationale}
        </p>
      )}

      {showPhotos && canShowPhotos && (
        <DecisionPhotos
          a={leftSrealityId as number}
          b={rightSrealityId as number}
          roomType={roomType}
        />
      )}
    </div>
  );
}

function DecisionPhotos({
  a,
  b,
  roomType,
}: {
  a: number;
  b: number;
  roomType: string | null;
}) {
  const q = useQuery({
    queryKey: ['dedup', 'decision-images', a, b, roomType],
    queryFn: () => getDedupDecisionImages({ a, b, room_type: roomType, per_side: 4 }),
    staleTime: 5 * 60_000,
  });
  if (q.isLoading) {
    return <p className="text-[0.72rem] text-[var(--color-ink-4)]">Načítám fotky…</p>;
  }
  const d = q.data?.data;
  if (!d) return null;
  return (
    <div className="grid grid-cols-2 gap-2 mt-0.5">
      <PhotoStrip side={d.left} />
      <PhotoStrip side={d.right} />
    </div>
  );
}

function PhotoStrip({
  side,
}: {
  side: { sreality_id: number; images: { sreality_url: string | null; storage_path: string | null }[]; fallback: boolean };
}) {
  return (
    <div className="flex flex-col gap-1">
      <div className="flex gap-1 overflow-x-auto">
        {side.images.length === 0 ? (
          <span className="text-[0.7rem] text-[var(--color-ink-4)]">bez fotek</span>
        ) : (
          side.images.map((img, i) => (
            <img
              key={i}
              src={imageSrc({ sreality_url: img.sreality_url ?? '', storage_path: img.storage_path })}
              alt=""
              loading="lazy"
              className="h-16 w-16 object-cover rounded-[var(--radius-xs)] border border-[var(--color-rule)] shrink-0"
            />
          ))
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
