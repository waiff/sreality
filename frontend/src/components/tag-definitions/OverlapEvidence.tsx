import type { NewDedupTag, TagNeighbour } from '@/lib/api';

/* The other half of the evidence: the tags whose labeled positives sit closest
 * to this one's in CLIP embedding space. This is how "these two are really one
 * tag" gets discovered — the operator sees the overlap before they try to write
 * the line that separates them. */
interface Props {
  neighbours: ReadonlyArray<TagNeighbour>;
  tagById: ReadonlyMap<number, NewDedupTag>;
  /* tag_ids already staged in the draft's confusable_with. */
  listedIds: ReadonlyArray<number>;
  onAddConfusable: (tagId: number) => void;
  loading: boolean;
  minPositives: number;
}

export default function OverlapEvidence({
  neighbours,
  tagById,
  listedIds,
  onAddConfusable,
  loading,
  minPositives,
}: Props) {
  const listed = new Set(listedIds);

  return (
    <section className="mt-6 border-t border-[var(--color-rule)] pt-4">
      <h2 className="text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
        Overlap evidence
      </h2>
      <p className="mt-1 text-[0.7rem] text-[var(--color-ink-4)]">
        Closest tags in CLIP embedding space — lower distance means the two tags' labeled
        images look alike. If you cannot write a does-not-count line separating them, they
        are one tag.
      </p>

      {loading && <p className="mt-3 text-sm text-[var(--color-ink-3)]">Loading…</p>}
      {!loading && neighbours.length === 0 && (
        <p className="mt-3 text-sm text-[var(--color-ink-3)]">
          Needs at least {minPositives} positives with CLIP embeddings to compare.
        </p>
      )}

      {neighbours.length > 0 && (
        <ul className="mt-3 space-y-1">
          {neighbours.map((n) => {
            const already = listed.has(n.tag_id);
            return (
              <li key={n.tag_id} className="flex items-baseline gap-2">
                <span
                  title={n.label}
                  className={[
                    'min-w-0 flex-1 truncate font-mono text-[0.78rem]',
                    tagById.get(n.tag_id)?.priority
                      ? 'text-[var(--color-brick)]'
                      : 'text-[var(--color-ink-2)]',
                  ].join(' ')}
                >
                  {n.label}
                </span>
                <span className="shrink-0 font-mono text-[0.7rem] tabular-nums text-[var(--color-ink-3)]">
                  distance {n.cosine_distance.toFixed(3)}
                </span>
                <span className="shrink-0 font-mono text-[0.7rem] tabular-nums text-[var(--color-ink-4)]">
                  {n.embedded_positive_count} pos
                </span>
                <button
                  type="button"
                  disabled={already}
                  onClick={() => onAddConfusable(n.tag_id)}
                  className={[
                    'shrink-0 px-1.5 py-0.5 text-[0.68rem] rounded-[var(--radius-xs)] border',
                    already
                      ? 'border-[var(--color-rule)] text-[var(--color-ink-4)]'
                      : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-copper)] hover:border-[var(--color-copper)]',
                  ].join(' ')}
                >
                  {already ? 'already listed' : 'Add to confusable'}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
