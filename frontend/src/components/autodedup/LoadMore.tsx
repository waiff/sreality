/* AUTODEDUP · the next page of a review list, one button for every queue. */

export default function LoadMore({
  list,
}: {
  list: { hasNextPage: boolean; fetchNextPage: () => void; isFetchingNextPage: boolean };
}) {
  if (!list.hasNextPage) return null;
  return (
    <div className="mt-4">
      <button
        type="button"
        onClick={list.fetchNextPage}
        disabled={list.isFetchingNextPage}
        className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-3 py-1.5 text-sm text-[var(--color-ink-2)] hover:text-[var(--color-ink)] disabled:opacity-50"
      >
        {list.isFetchingNextPage ? 'Načítám…' : 'Načíst další'}
      </button>
    </div>
  );
}
