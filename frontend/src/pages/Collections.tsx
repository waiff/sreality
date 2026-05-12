import { useState } from 'react';
import { Link } from 'react-router-dom';
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query';
import {
  createCollection,
  deleteCollection,
  listCollections,
} from '@/lib/api';
import { curationKeys } from '@/lib/queries';
import { fmtCount, fmtRelative, fmtAbsolute } from '@/lib/format';
import type { Collection } from '@/lib/types';

export default function Collections() {
  const qc = useQueryClient();

  const listQ = useQuery<{ data: Collection[]; total: number }, Error>({
    queryKey: curationKeys.collections,
    queryFn: listCollections,
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  });

  const items = listQ.data?.data ?? [];

  return (
    <div className="px-6 py-8 max-w-4xl mx-auto">
      <Header total={listQ.data?.total ?? null} />
      <div className="mt-7">
        <NewCollectionForm
          existing={items.map((c) => c.name.toLowerCase())}
          onCreated={() => qc.invalidateQueries({ queryKey: curationKeys.collections })}
        />
      </div>
      <div className="mt-9">
        {listQ.isLoading && !listQ.data ? (
          <div className="text-sm text-[var(--color-ink-3)]">Loading…</div>
        ) : listQ.error ? (
          <div className="text-sm text-[var(--color-brick)]">
            Failed to load: {listQ.error.message}
          </div>
        ) : items.length === 0 ? (
          <EmptyState />
        ) : (
          <CollectionList items={items} />
        )}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Header                                                                     */
/* -------------------------------------------------------------------------- */

function Header({ total }: { total: number | null }) {
  return (
    <header>
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        Collections
      </p>
      <h1
        className="mt-1.5 text-[2.1rem] leading-tight"
        style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
      >
        Curated lists
      </h1>
      <p className="mt-2 text-sm text-[var(--color-ink-2)]">
        Named groups of listings. A listing can live in many collections.{' '}
        {total != null && total > 0 && (
          <span className="text-[var(--color-ink-3)]">
            {fmtCount(total)} {total === 1 ? 'collection' : 'collections'}.
          </span>
        )}
      </p>
    </header>
  );
}

/* -------------------------------------------------------------------------- */
/* New-collection form                                                        */
/* -------------------------------------------------------------------------- */

function NewCollectionForm({
  existing,
  onCreated,
}: {
  existing: string[];
  onCreated: () => void;
}) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [error, setError] = useState<string | null>(null);

  const mut = useMutation({
    mutationFn: (input: { name: string; description: string | null }) =>
      createCollection(input),
    onSuccess: () => {
      setName('');
      setDescription('');
      setError(null);
      onCreated();
    },
    onError: (err: Error) => {
      setError(err.message || 'Failed to create');
    },
  });

  const trimmed = name.trim();
  const dup = existing.includes(trimmed.toLowerCase());
  const disabled = trimmed.length === 0 || dup || mut.isPending;

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (disabled) return;
        mut.mutate({
          name: trimmed,
          description: description.trim() === '' ? null : description.trim(),
        });
      }}
      className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-4 py-4"
    >
      <p className="text-[0.65rem] tracking-[0.22em] uppercase text-[var(--color-ink-2)] font-medium">
        New collection
      </p>
      <div className="mt-3 flex flex-col sm:flex-row gap-3">
        <input
          type="text"
          value={name}
          onChange={(e) => {
            setName(e.target.value);
            setError(null);
          }}
          placeholder="Name (e.g. Shortlist)"
          maxLength={200}
          className="flex-1 min-w-0 px-3 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)]"
        />
        <input
          type="text"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="Optional description"
          className="flex-1 min-w-0 px-3 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)]"
        />
        <button
          type="submit"
          disabled={disabled}
          className="shrink-0 px-4 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        >
          {mut.isPending ? 'Creating…' : 'Create'}
        </button>
      </div>
      {dup && (
        <p className="mt-2 text-[0.75rem] text-[var(--color-brick)]">
          A collection named "{trimmed}" already exists.
        </p>
      )}
      {error && !dup && (
        <p className="mt-2 text-[0.75rem] text-[var(--color-brick)]">{error}</p>
      )}
    </form>
  );
}

/* -------------------------------------------------------------------------- */
/* List                                                                       */
/* -------------------------------------------------------------------------- */

function CollectionList({ items }: { items: Collection[] }) {
  return (
    <ul className="divide-y divide-[var(--color-rule-soft)] border-t border-b border-[var(--color-rule-soft)]">
      {items.map((c) => (
        <li key={c.id}>
          <CollectionRow c={c} />
        </li>
      ))}
    </ul>
  );
}

function CollectionRow({ c }: { c: Collection }) {
  const qc = useQueryClient();
  const [confirming, setConfirming] = useState(false);

  const del = useMutation({
    mutationFn: () => deleteCollection(c.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: curationKeys.collections });
    },
  });

  return (
    <div className="py-4 flex items-baseline gap-5">
      <div className="min-w-0 flex-1">
        <Link
          to={`/collection/${c.id}`}
          className="block text-base text-[var(--color-ink)] hover:text-[var(--color-copper)] hover:underline underline-offset-2 truncate"
        >
          {c.name}
        </Link>
        {c.description && (
          <p className="mt-1 text-sm text-[var(--color-ink-3)] truncate">
            {c.description}
          </p>
        )}
        <p className="mt-1 text-[0.7rem] tracking-wide text-[var(--color-ink-4)]">
          Updated{' '}
          <span className="cursor-help" title={fmtAbsolute(c.updated_at)}>
            {fmtRelative(c.updated_at)}
          </span>
        </p>
      </div>
      <div className="shrink-0 text-right">
        <p className="font-mono tabular-nums text-sm text-[var(--color-ink-2)]">
          {fmtCount(c.listing_count)}
        </p>
        <p className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)] mt-0.5">
          {c.listing_count === 1 ? 'listing' : 'listings'}
        </p>
      </div>
      <div className="shrink-0">
        {confirming ? (
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => del.mutate()}
              disabled={del.isPending}
              className="px-2.5 py-1 text-[0.7rem] tracking-wide rounded-[var(--radius-sm)] bg-[var(--color-brick-soft)] text-[var(--color-brick)] hover:bg-[var(--color-brick)]/15 disabled:opacity-50"
            >
              {del.isPending ? 'Deleting…' : 'Confirm'}
            </button>
            <button
              type="button"
              onClick={() => setConfirming(false)}
              className="px-2.5 py-1 text-[0.7rem] tracking-wide text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]"
            >
              Cancel
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => setConfirming(true)}
            className="px-2.5 py-1 text-[0.7rem] tracking-wide text-[var(--color-ink-3)] hover:text-[var(--color-brick)] transition-colors"
          >
            Delete
          </button>
        )}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Empty                                                                      */
/* -------------------------------------------------------------------------- */

function EmptyState() {
  return (
    <div className="px-6 py-12 text-center border border-dashed border-[var(--color-rule)] rounded-[var(--radius-md)]">
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        No collections yet
      </p>
      <p className="mt-2 text-sm text-[var(--color-ink-2)]">
        Use the form above to start one. Then add listings from any listing's
        detail page.
      </p>
    </div>
  );
}
