import type { TagContentsOrder, TagPositiveImage } from '@/lib/api';

export type ContentsOrder = TagContentsOrder;

/* The server returns the rows already outlier-first, so that view is identity.
 * 'recent' is a VIEW over the same fetched rows, re-sorted on the SAME total
 * order the server's recent read uses — (updated_at DESC, image_id DESC) — the
 * unique tiebreaker included, because a bare timestamp sort reshuffles under the
 * operator. Both sides serialise the same ISO shape, so a string compare is the
 * timestamp compare. Flipping the order refetches nothing. */
export function orderPositives(
  rows: ReadonlyArray<TagPositiveImage>,
  order: ContentsOrder,
): ReadonlyArray<TagPositiveImage> {
  if (order === 'outlier_first') return rows;
  return [...rows].sort(
    (a, b) =>
      (a.updated_at < b.updated_at ? 1 : a.updated_at > b.updated_at ? -1 : 0) ||
      b.image_id - a.image_id,
  );
}

const OUTLIER_OPTION = {
  key: 'outlier_first' as ContentsOrder,
  label: 'Least like the rest',
  title:
    "Farthest first from this tag's own centre in CLIP space — where an image that does not belong tends to sit. A rank inside this tag only.",
};

/* The recent view's promise rests on the WINDOW, not only on the sort. The
 * server applies its LIMIT *after* the distance sort, so on a tag holding more
 * positives than the cap the fetched rows are the N FARTHEST — re-sorting those
 * by time yields the newest OF THOSE, which is not the tag's newest and can miss
 * work done this morning (a typical new image sits NEAR the centre, i.e. outside
 * the window). One fetch per tag is the deliberate design — a second cache entry
 * per order is a second thing every retag has to keep true, and flipping would
 * blank the grid — so the button stops making a promise the fetched rows cannot
 * keep instead. Only ever narrowed when the window was actually distance-chosen. */
const recentOption = (windowed: boolean, limit: number) =>
  windowed
    ? {
        key: 'recent' as ContentsOrder,
        label: `Newest of these ${limit}`,
        title: `Most recently labeled among the ${limit} fetched. This tag holds more than ${limit} positives and these ${limit} were selected by DISTANCE, so they are not the tag's ${limit} newest.`,
      }
    : {
        key: 'recent' as ContentsOrder,
        label: 'Newest first',
        title: 'Most recently labeled first — what moved since the last sitting.',
      };

/* Which end of the tag the operator is reading from. Sits on the gallery's own
 * header line, not in a toolbar somewhere else: the order IS the reading, and a
 * control for it belongs beside the count it reorders. */
export default function ContentsOrderControl({
  order,
  onOrderChange,
  outlierApplied,
  centroidPositives,
  minPositives,
  truncated,
  limit,
}: {
  order: ContentsOrder;
  onOrderChange: (order: ContentsOrder) => void;
  /* The SERVER's verdict (response `order`), never re-derived here. */
  outlierApplied: boolean;
  /* null = never computed (a failed read, or a `recent` response), which is NOT
   * the same fact as "this tag has none" and must never be rendered as one. */
  centroidPositives: number | null;
  minPositives: number;
  /* The server returned a full page, so the tag holds more than is on screen. */
  truncated: boolean;
  limit: number;
}) {
  const options = [OUTLIER_OPTION, recentOption(truncated && outlierApplied, limit)];
  return (
    <>
      <div
        role="group"
        aria-label="Order this tag's images"
        className="ml-auto flex items-center gap-1"
      >
        {options.map((o) => {
          const active = order === o.key;
          const disabled = o.key === 'outlier_first' && !outlierApplied;
          return (
            <button
              key={o.key}
              type="button"
              aria-pressed={active}
              disabled={disabled}
              title={o.title}
              onClick={() => onOrderChange(o.key)}
              className={[
                'px-1.5 py-0.5 text-[0.68rem] rounded-[var(--radius-xs)] border transition-colors disabled:opacity-40',
                active
                  ? 'border-[var(--color-copper)] text-[var(--color-copper)]'
                  : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]',
              ].join(' ')}
            >
              {o.label}
            </button>
          );
        })}
      </div>
      {/* WHAT the order rests on, on screen whichever side of the floor the tag
          sits. Above it, because a centroid over 5 images and one over 200 are
          otherwise indistinguishable here — same active button, same badges,
          same ranks — and at five, each image is a fifth of the centre it is
          then measured against. Below it, because a silent fallback to the old
          sort is the failure this floor exists to prevent.

          Deliberately NOT worded as OverlapEvidence's floor note despite the
          identical number: that panel's centroids carry no source filter and
          count every embedded positive, this one counts human-verified ones
          only (`machine` is a writable source), so the two are different
          populations of the same-sounding quantity and must not read as one.
          And `centroid_positives` is dropped from the sentence when it is null
          rather than shown as 0 — a read that failed took no measurement, and
          "this tag has 0" is a data-shaped diagnosis of a transport failure on
          the one page whose whole claim is showing a tag's real contents. */}
      {outlierApplied && centroidPositives != null && (
        <p className="basis-full text-[0.7rem] text-[var(--color-ink-4)]">
          Distance measured against this tag's {centroidPositives} human-verified positives — a
          rank inside this tag only.
        </p>
      )}
      {!outlierApplied && (
        <p className="basis-full text-[0.7rem] text-[var(--color-ink-4)]">
          Needs at least {minPositives} human-verified positives with a CLIP embedding to sort by
          distance
          {centroidPositives == null ? '' : ` — this tag has ${centroidPositives}`}. Showing
          newest first.
        </p>
      )}
    </>
  );
}

/* WHY a tile sits where it does. A distance, named as one and rendered as one —
 * the same discipline OverlapEvidence keeps, for the same reason: it ranks
 * images inside ONE tag and transfers to no other. Deliberately uniform: the
 * ORDER is the signal, the number is the audit trail, and tinting "the worst
 * ones" would assert a judgement the system cannot make. */
export function OutlierRankBadge({
  row,
  shown,
}: {
  row: TagPositiveImage;
  shown: boolean;
}) {
  if (!shown) return null;
  const d = row.centroid_distance;
  const cls =
    'absolute right-1 bottom-1 px-1 py-px font-mono text-[0.6rem] leading-none tabular-nums rounded-[var(--radius-xs)] border border-[var(--color-rule-strong)] bg-[var(--color-paper)]/85 text-[var(--color-ink-3)]';
  if (d == null)
    return (
      <span
        className={cls}
        title="No CLIP embedding for this image, so it cannot be placed in this order — it sits at the end. Not an outlier, just unplaceable."
      >
        no embedding
      </span>
    );
  return (
    <span
      className={cls}
      title={`Cosine distance ${d.toFixed(3)} from this tag's centre${
        row.distance_rank != null ? ` — #${row.distance_rank} farthest here` : ''
      }. A distance ranks images inside ONE tag; it never compares across tags.`}
    >
      d {d.toFixed(2)}
    </span>
  );
}
