import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import {
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query';
import {
  ApiError,
  deleteCollection,
  getCollection,
  removePropertyFromCollection,
  updateCollection,
} from '@/lib/api';
import { curationKeys } from '@/lib/queries';
import { listingPath } from '@/lib/listingUrl';
import { usePageTitle } from '@/lib/pageTitle';
import { DeliveryChannelsPicker } from '@/components/DeliveryChannelsPicker';
import {
  fmtAbsolute,
  fmtArea,
  fmtCount,
  fmtCzk,
  fmtRelative,
} from '@/lib/format';
import type {
  Collection,
  CollectionPropertyRow,
  CollectionWithProperties,
} from '@/lib/types';

export default function CollectionDetail() {
  const { id: idParam } = useParams();
  const id = idParam && /^\d+$/.test(idParam) ? Number(idParam) : null;

  const q = useQuery<CollectionWithProperties, Error>({
    queryKey: id != null ? curationKeys.collection(id) : ['curation', 'collection', 'invalid'],
    queryFn: () => getCollection(id as number),
    enabled: id != null,
    staleTime: 30_000,
  });

  usePageTitle(q.data ? q.data.collection.name : null);

  if (id == null) {
    return <NotFoundState reason="invalid" id={idParam ?? null} />;
  }

  if (q.isLoading) {
    return (
      <Page>
        <Crumb />
        <div className="mt-8 text-sm text-[var(--color-ink-3)]">Loading…</div>
      </Page>
    );
  }

  if (q.error) {
    const apiErr = q.error as ApiError | Error;
    if (apiErr instanceof ApiError && apiErr.status === 404) {
      return <NotFoundState reason="missing" id={String(id)} />;
    }
    return (
      <Page>
        <Crumb />
        <div className="mt-8 text-sm text-[var(--color-brick)]">
          Failed to load: {q.error.message}
        </div>
      </Page>
    );
  }

  if (!q.data) {
    return <NotFoundState reason="missing" id={String(id)} />;
  }

  const { collection, properties } = q.data;
  return (
    <Page>
      <Crumb />
      <Header collection={collection} />
      <Hairline />
      <EditBlock collection={collection} />
      <Hairline />
      <MonitoringBlock collection={collection} />
      <Hairline />
      <PropertiesBlock collectionId={collection.id} properties={properties} />
    </Page>
  );
}

/* -------------------------------------------------------------------------- */
/* Layout primitives                                                          */
/* -------------------------------------------------------------------------- */

function Page({ children }: { children: React.ReactNode }) {
  return <div className="px-6 py-8 max-w-5xl mx-auto">{children}</div>;
}

function Crumb() {
  return (
    <Link
      to="/collections"
      className="inline-flex items-center gap-1.5 text-[0.75rem] tracking-wide text-[var(--color-ink-3)] hover:text-[var(--color-copper)] transition-colors"
    >
      <BackArrow />
      <span>All collections</span>
    </Link>
  );
}

function Hairline() {
  return <div className="my-7 h-px bg-[var(--color-rule)]" />;
}

/* -------------------------------------------------------------------------- */
/* Header                                                                     */
/* -------------------------------------------------------------------------- */

function Header({ collection }: { collection: Collection }) {
  return (
    <div className="mt-5">
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        Collection
      </p>
      <h1
        className="mt-1.5 text-[2.6rem] leading-[1.05]"
        style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
      >
        {collection.name}
      </h1>
      {collection.description && (
        <p className="mt-2 text-sm text-[var(--color-ink-2)] max-w-2xl">
          {collection.description}
        </p>
      )}
      <p className="mt-2 text-[0.7rem] tracking-wide text-[var(--color-ink-4)]">
        <span className="font-mono tabular-nums text-[var(--color-ink-3)]">
          {fmtCount(collection.listing_count)}
        </span>{' '}
        {collection.listing_count === 1 ? 'property' : 'properties'} · updated{' '}
        <span className="cursor-help" title={fmtAbsolute(collection.updated_at)}>
          {fmtRelative(collection.updated_at)}
        </span>
      </p>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Edit block (rename / description) + delete                                 */
/* -------------------------------------------------------------------------- */

function EditBlock({ collection }: { collection: Collection }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [name, setName] = useState(collection.name);
  const [description, setDescription] = useState(collection.description ?? '');
  const [error, setError] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  const dirty =
    name.trim() !== collection.name ||
    (description.trim() === '' ? null : description.trim()) !==
      collection.description;

  const save = useMutation({
    mutationFn: () =>
      updateCollection(collection.id, {
        name: name.trim(),
        description: description.trim() === '' ? null : description.trim(),
      }),
    onSuccess: () => {
      setError(null);
      qc.invalidateQueries({ queryKey: curationKeys.collection(collection.id) });
      qc.invalidateQueries({ queryKey: curationKeys.collections });
    },
    onError: (err: Error) => setError(err.message || 'Failed to save'),
  });

  const del = useMutation({
    mutationFn: () => deleteCollection(collection.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: curationKeys.collections });
      navigate('/collections', { replace: true });
    },
    onError: (err: Error) => setError(err.message || 'Failed to delete'),
  });

  return (
    <div>
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
        Edit
      </p>
      <div className="mt-3 flex flex-col gap-2.5 max-w-2xl">
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          maxLength={200}
          placeholder="Name"
          disabled={collection.is_system}
          title={collection.is_system ? "The default collection can't be renamed." : undefined}
          className="px-3 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] focus:outline-none focus:border-[var(--color-rule-strong)] disabled:opacity-50 disabled:cursor-not-allowed"
        />
        <input
          type="text"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="Description (optional)"
          className="px-3 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)]"
        />
        <div className="flex items-center gap-3">
          <button
            type="button"
            disabled={!dirty || save.isPending || name.trim().length === 0}
            onClick={() => save.mutate()}
            className="px-4 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            {save.isPending ? 'Saving…' : 'Save changes'}
          </button>
          <div className="ml-auto">
            {collection.is_system ? (
              <span className="text-[0.72rem] text-[var(--color-ink-4)]">
                Default collection — can't be renamed or deleted.
              </span>
            ) : confirmingDelete ? (
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => del.mutate()}
                  disabled={del.isPending}
                  className="px-3 py-1.5 text-[0.75rem] tracking-wide rounded-[var(--radius-sm)] bg-[var(--color-brick-soft)] text-[var(--color-brick)] hover:bg-[var(--color-brick)]/15 disabled:opacity-50"
                >
                  {del.isPending ? 'Deleting…' : `Delete "${collection.name}"`}
                </button>
                <button
                  type="button"
                  onClick={() => setConfirmingDelete(false)}
                  className="px-2.5 py-1 text-[0.7rem] tracking-wide text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]"
                >
                  Cancel
                </button>
              </div>
            ) : (
              <button
                type="button"
                onClick={() => setConfirmingDelete(true)}
                className="text-[0.75rem] tracking-wide text-[var(--color-ink-3)] hover:text-[var(--color-brick)] transition-colors"
              >
                Delete collection
              </button>
            )}
          </div>
        </div>
        {error && (
          <p className="text-[0.75rem] text-[var(--color-brick)]">{error}</p>
        )}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Monitoring toggle                                                          */
/* -------------------------------------------------------------------------- */

function MonitoringBlock({ collection }: { collection: Collection }) {
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);

  const invalidate = () => {
    setError(null);
    qc.invalidateQueries({ queryKey: curationKeys.collection(collection.id) });
    qc.invalidateQueries({ queryKey: curationKeys.collections });
  };

  const toggle = useMutation({
    mutationFn: (next: boolean) =>
      updateCollection(collection.id, { monitoring_enabled: next }),
    onSuccess: invalidate,
    onError: (err: Error) => setError(err.message || 'Failed to update'),
  });

  const channels = useMutation({
    mutationFn: (next: string[]) =>
      updateCollection(collection.id, { notify_channels: next }),
    onSuccess: invalidate,
    onError: (err: Error) => setError(err.message || 'Failed to update'),
  });

  const on = collection.monitoring_enabled;

  return (
    <div>
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
        Monitoring
      </p>
      <div className="mt-3 flex items-start gap-3 max-w-2xl">
        <button
          type="button"
          role="switch"
          aria-checked={on}
          disabled={toggle.isPending}
          onClick={() => toggle.mutate(!on)}
          className={[
            'mt-0.5 relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors disabled:opacity-50',
            on ? 'bg-[var(--color-copper)]' : 'bg-[var(--color-rule-strong)]',
          ].join(' ')}
        >
          <span
            className={[
              'inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform',
              on ? 'translate-x-[1.15rem]' : 'translate-x-[0.15rem]',
            ].join(' ')}
          />
        </button>
        <div className="text-sm text-[var(--color-ink-2)]">
          <p>
            {on
              ? 'On — a property in this collection raises an alert when its price changes or it is delisted.'
              : 'Off — properties here are not monitored for changes.'}
          </p>
          <p className="mt-1 text-[0.78rem] text-[var(--color-ink-4)]">
            Alerts always appear in the in-app Notifications feed.
          </p>
        </div>
      </div>
      {on && (
        <div className="mt-4 max-w-2xl">
          <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
            Delivery
          </p>
          <div className="mt-2">
            <DeliveryChannelsPicker
              value={collection.notify_channels ?? []}
              onChange={(next) => channels.mutate(next)}
              disabled={channels.isPending}
            />
          </div>
        </div>
      )}
      {error && (
        <p className="mt-2 text-[0.75rem] text-[var(--color-brick)]">{error}</p>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Listings table                                                             */
/* -------------------------------------------------------------------------- */

function PropertiesBlock({
  collectionId,
  properties,
}: {
  collectionId: number;
  properties: CollectionPropertyRow[];
}) {
  return (
    <div>
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
        Properties
      </p>
      {properties.length === 0 ? (
        <p className="mt-3 text-sm text-[var(--color-ink-3)]">
          No properties yet. Use the "Add to collection" picker on a listing's
          detail page to put something here.
        </p>
      ) : (
        <div className="mt-3 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-[var(--color-paper-2)] border-b border-[var(--color-rule)]">
                <tr>
                  <Th align="left">ID</Th>
                  <Th align="left">District</Th>
                  <Th align="left">Type</Th>
                  <Th align="right">Area</Th>
                  <Th align="right">Price</Th>
                  <Th align="left">Last seen</Th>
                  <Th align="left">Status</Th>
                  <Th align="left">Added</Th>
                  <Th align="right">{''}</Th>
                </tr>
              </thead>
              <tbody>
                {properties.map((row) => (
                  <PropertyRowView
                    key={row.property_id}
                    row={row}
                    collectionId={collectionId}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

function Th({
  align,
  children,
}: {
  align: 'left' | 'right';
  children: React.ReactNode;
}) {
  return (
    <th
      scope="col"
      className={[
        'px-3 py-2.5 text-[0.7rem] tracking-[0.14em] uppercase font-medium text-[var(--color-ink-3)]',
        align === 'right' ? 'text-right' : 'text-left',
      ].join(' ')}
    >
      {children}
    </th>
  );
}

function PropertyRowView({
  row,
  collectionId,
}: {
  row: CollectionPropertyRow;
  collectionId: number;
}) {
  const qc = useQueryClient();

  const remove = useMutation({
    mutationFn: () => removePropertyFromCollection(collectionId, row.property_id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: curationKeys.collection(collectionId) });
      qc.invalidateQueries({ queryKey: curationKeys.collections });
      qc.invalidateQueries({
        queryKey: curationKeys.propertyCollections(row.property_id),
      });
    },
  });

  return (
    <tr className="border-b border-[var(--color-rule-soft)] last:border-b-0 hover:bg-[var(--color-copper-soft)]/40 transition-colors">
      <td className="px-3 py-2.5 align-middle font-mono tabular-nums text-[var(--color-ink-3)]">
        <Link
          to={listingPath(row.sreality_id)}
          className="hover:text-[var(--color-copper)] hover:underline underline-offset-2"
        >
          {row.sreality_id}
        </Link>
      </td>
      <td className="px-3 py-2.5 align-middle text-[var(--color-ink-2)]">
        {row.district ?? <span className="text-[var(--color-ink-4)]">—</span>}
      </td>
      <td className="px-3 py-2.5 align-middle font-mono tabular-nums text-[var(--color-ink-2)]">
        {row.disposition ?? <span className="text-[var(--color-ink-4)]">—</span>}
      </td>
      <td className="px-3 py-2.5 align-middle text-right font-mono tabular-nums text-[var(--color-ink-2)]">
        {row.area_m2 != null ? fmtArea(row.area_m2) : <span className="text-[var(--color-ink-4)]">—</span>}
      </td>
      <td className="px-3 py-2.5 align-middle text-right font-mono tabular-nums text-[var(--color-ink)]">
        {fmtCzk(row.price_czk)}
      </td>
      <td
        className="px-3 py-2.5 align-middle text-[var(--color-ink-2)] cursor-help"
        title={fmtAbsolute(row.last_seen_at)}
      >
        {fmtRelative(row.last_seen_at)}
      </td>
      <td className="px-3 py-2.5 align-middle">
        <StatusDot active={row.is_active} />
      </td>
      <td
        className="px-3 py-2.5 align-middle text-[var(--color-ink-3)] text-[0.78rem] cursor-help"
        title={fmtAbsolute(row.added_at)}
      >
        {fmtRelative(row.added_at)}
      </td>
      <td className="px-3 py-2.5 align-middle text-right">
        <button
          type="button"
          onClick={() => remove.mutate()}
          disabled={remove.isPending}
          aria-label={`Remove ${row.sreality_id} from collection`}
          title="Remove from collection"
          className="text-[0.7rem] tracking-wide text-[var(--color-ink-4)] hover:text-[var(--color-brick)] disabled:opacity-50 transition-colors"
        >
          {remove.isPending ? '…' : 'Remove'}
        </button>
      </td>
    </tr>
  );
}

function StatusDot({ active }: { active: boolean }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-[0.7rem] tracking-wide">
      <span
        className="w-1.5 h-1.5 rounded-full"
        style={{ background: active ? 'var(--color-sage)' : 'var(--color-brick)' }}
        aria-hidden
      />
      <span className="text-[var(--color-ink-3)]">
        {active ? 'Active' : 'Inactive'}
      </span>
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* Not-found state                                                            */
/* -------------------------------------------------------------------------- */

function NotFoundState({
  reason,
  id,
}: {
  reason: 'invalid' | 'missing';
  id: string | null;
}) {
  return (
    <Page>
      <Crumb />
      <div className="mt-12">
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          Not found
        </p>
        <h1
          className="mt-2 text-2xl"
          style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
        >
          {reason === 'invalid' ? (
            'No collection requested'
          ) : (
            <>
              No collection with id{' '}
              <span className="font-mono tabular-nums text-[var(--color-ink-2)]">
                {id}
              </span>
            </>
          )}
        </h1>
        <p className="mt-3 text-sm text-[var(--color-ink-3)]">
          The id may be wrong, or the collection was deleted.
          <Link
            to="/collections"
            className="ml-1 text-[var(--color-copper)] hover:underline"
          >
            All collections
          </Link>
          .
        </p>
      </div>
    </Page>
  );
}

function BackArrow() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
      <polyline
        points="5.5,1.5 1.5,5 5.5,8.5"
        stroke="currentColor"
        strokeWidth="1.25"
        fill="none"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <line
        x1="1.5"
        y1="5"
        x2="9"
        y2="5"
        stroke="currentColor"
        strokeWidth="1.25"
        strokeLinecap="round"
      />
    </svg>
  );
}
