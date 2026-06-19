/* Collections + tags + notes for a single PROPERTY. Wired into ListingDetail.
 *
 * Curation is property-grain (migration 202): a tag / collection membership /
 * note describes the real-world property, so this block operates on the
 * listing's `property_id`. The viewed `sreality_id` is recorded as a note's
 * `origin_listing_id` (display provenance — "written while viewing this advert").
 *
 * Reads come from two places by design:
 *   - The "which collections / tags exist" indices use the bearer-gated
 *     FastAPI service so listing_count + ordering live in one place.
 *   - The "does THIS property belong to X" reverse-index reads pull from the
 *     *_public Supabase views via the anon key. Writes always go through the API.
 */

import { useMemo, useRef, useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import {
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query';
import {
  ApiError,
  addPipelineCard,
  addPropertiesToCollection,
  attachTag,
  createPropertyNote,
  createTag,
  detachTag,
  listCollections,
  listPropertyNotes,
  listTags,
  removePipelineCard,
  removePropertyFromCollection,
} from '@/lib/api';
import {
  curationKeys,
  fetchPropertyCollectionIds,
  fetchPropertyPipeline,
  fetchPropertyTagIds,
  pipelineKeys,
} from '@/lib/queries';
import { fmtAbsolute, fmtRelative } from '@/lib/format';
import type { Collection, Note, Tag, TagColor } from '@/lib/types';
import { TAG_COLORS } from '@/lib/types';
import TagEditPopover from '@/components/curation/TagEditPopover';

export default function CurationBlock({
  property_id,
  sreality_id,
}: {
  property_id: number;
  sreality_id: number;
}) {
  return (
    <div className="space-y-7">
      <PipelineRow property_id={property_id} />
      <CollectionsRow property_id={property_id} />
      <TagsRow property_id={property_id} />
      <NotesRow property_id={property_id} sreality_id={sreality_id} />
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Pipeline (bookmark / interested = the entry stage)                         */
/* -------------------------------------------------------------------------- */

function PipelineRow({ property_id }: { property_id: number }) {
  const qc = useQueryClient();

  const cardQ = useQuery({
    queryKey: pipelineKeys.card(property_id),
    queryFn: () => fetchPropertyPipeline(property_id),
    staleTime: 30_000,
  });
  const card = cardQ.data ?? null;
  const inPipeline = card != null;

  const invalidate = () =>
    qc.invalidateQueries({ queryKey: pipelineKeys.card(property_id) });

  const add = useMutation({
    mutationFn: () => addPipelineCard(property_id),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: () => removePipelineCard(property_id),
    onSuccess: invalidate,
  });
  const pending = add.isPending || remove.isPending;

  const fg = card?.stage_color
    ? `var(--color-tag-${card.stage_color})`
    : 'var(--color-copper)';
  const bg = card?.stage_color
    ? `var(--color-tag-${card.stage_color}-soft)`
    : 'var(--color-copper-soft)';

  return (
    <div>
      <SectionLabel>Pipeline</SectionLabel>
      <div className="mt-3">
        <button
          type="button"
          onClick={() => (inPipeline ? remove.mutate() : add.mutate())}
          disabled={pending || cardQ.isLoading}
          aria-pressed={inPipeline}
          title={inPipeline ? 'Odebrat z pipeline' : 'Přidat do pipeline'}
          className={[
            'inline-flex items-center gap-1.5 px-2.5 py-1 text-[0.78rem] rounded-[var(--radius-sm)] border transition-colors disabled:opacity-60',
            inPipeline ? '' : 'bg-[var(--color-paper-2)] border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)]',
          ].join(' ')}
          style={
            inPipeline
              ? { background: bg, color: fg, borderColor: fg }
              : undefined
          }
        >
          <span aria-hidden className="leading-none">{inPipeline ? '★' : '☆'}</span>
          <span>{inPipeline ? (card?.stage_label ?? 'V pipeline') : 'Přidat do pipeline'}</span>
        </button>
      </div>
    </div>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
      {children}
    </p>
  );
}

/* -------------------------------------------------------------------------- */
/* Collections                                                                */
/* -------------------------------------------------------------------------- */

function CollectionsRow({ property_id }: { property_id: number }) {
  const qc = useQueryClient();

  const allQ = useQuery({
    queryKey: curationKeys.collections,
    queryFn: listCollections,
    staleTime: 30_000,
  });

  const membershipQ = useQuery({
    queryKey: curationKeys.propertyCollections(property_id),
    queryFn: () => fetchPropertyCollectionIds(property_id),
    staleTime: 30_000,
  });

  const memberIds = useMemo(
    () => new Set(membershipQ.data ?? []),
    [membershipQ.data],
  );

  const collections = allQ.data?.data ?? [];

  const add = useMutation({
    mutationFn: (collection_id: number) =>
      addPropertiesToCollection(collection_id, [property_id]),
    onSuccess: (_, collection_id) => {
      qc.invalidateQueries({
        queryKey: curationKeys.propertyCollections(property_id),
      });
      qc.invalidateQueries({ queryKey: curationKeys.collections });
      qc.invalidateQueries({ queryKey: curationKeys.collection(collection_id) });
    },
  });

  const remove = useMutation({
    mutationFn: (collection_id: number) =>
      removePropertyFromCollection(collection_id, property_id),
    onSuccess: (_, collection_id) => {
      qc.invalidateQueries({
        queryKey: curationKeys.propertyCollections(property_id),
      });
      qc.invalidateQueries({ queryKey: curationKeys.collections });
      qc.invalidateQueries({ queryKey: curationKeys.collection(collection_id) });
    },
  });

  return (
    <div>
      <div className="flex items-baseline justify-between">
        <SectionLabel>Collections</SectionLabel>
        <Link
          to="/collections"
          className="text-[0.7rem] tracking-wide text-[var(--color-ink-3)] hover:text-[var(--color-copper)] transition-colors"
        >
          Manage →
        </Link>
      </div>

      {allQ.isLoading ? (
        <p className="mt-3 text-sm text-[var(--color-ink-3)]">Loading…</p>
      ) : allQ.error ? (
        <p className="mt-3 text-sm text-[var(--color-brick)]">
          Failed to load collections: {(allQ.error as Error).message}
        </p>
      ) : collections.length === 0 ? (
        <p className="mt-3 text-sm text-[var(--color-ink-3)]">
          No collections yet.{' '}
          <Link
            to="/collections"
            className="text-[var(--color-copper)] hover:underline"
          >
            Start one
          </Link>
          .
        </p>
      ) : (
        <ul className="mt-3 flex flex-wrap gap-1.5">
          {collections.map((c) => (
            <li key={c.id}>
              <CollectionToggle
                c={c}
                member={memberIds.has(c.id)}
                pending={add.isPending || remove.isPending}
                onAdd={() => add.mutate(c.id)}
                onRemove={() => remove.mutate(c.id)}
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function CollectionToggle({
  c,
  member,
  pending,
  onAdd,
  onRemove,
}: {
  c: Collection;
  member: boolean;
  pending: boolean;
  onAdd: () => void;
  onRemove: () => void;
}) {
  const cls = member
    ? 'bg-[var(--color-copper-soft)] text-[var(--color-copper)] border-[var(--color-copper)]/40'
    : 'bg-[var(--color-paper-2)] text-[var(--color-ink-3)] border-[var(--color-rule)] hover:text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)]';
  return (
    <button
      type="button"
      onClick={member ? onRemove : onAdd}
      disabled={pending}
      aria-pressed={member}
      className={[
        'inline-flex items-center gap-1.5 px-2.5 py-1 text-[0.78rem] rounded-[var(--radius-sm)] border transition-colors disabled:opacity-60',
        cls,
      ].join(' ')}
    >
      <span aria-hidden className="text-[0.65rem] leading-none">
        {member ? '✓' : '+'}
      </span>
      <span className="truncate max-w-[14rem]">{c.name}</span>
    </button>
  );
}

/* -------------------------------------------------------------------------- */
/* Tags                                                                       */
/* -------------------------------------------------------------------------- */

function TagsRow({ property_id }: { property_id: number }) {
  const qc = useQueryClient();

  const allQ = useQuery({
    queryKey: curationKeys.tags,
    queryFn: listTags,
    staleTime: 30_000,
  });

  const membershipQ = useQuery({
    queryKey: curationKeys.propertyTags(property_id),
    queryFn: () => fetchPropertyTagIds(property_id),
    staleTime: 30_000,
  });

  const memberIds = useMemo(
    () => new Set(membershipQ.data ?? []),
    [membershipQ.data],
  );

  const tags = allQ.data?.data ?? [];

  const attach = useMutation({
    mutationFn: (tag_id: number) => attachTag(property_id, tag_id),
    onSuccess: () => {
      qc.invalidateQueries({
        queryKey: curationKeys.propertyTags(property_id),
      });
      qc.invalidateQueries({ queryKey: curationKeys.tags });
    },
  });

  const detach = useMutation({
    mutationFn: (tag_id: number) => detachTag(property_id, tag_id),
    onSuccess: () => {
      qc.invalidateQueries({
        queryKey: curationKeys.propertyTags(property_id),
      });
      qc.invalidateQueries({ queryKey: curationKeys.tags });
    },
  });

  const memberTags = tags.filter((t) => memberIds.has(t.id));
  const otherTags = tags.filter((t) => !memberIds.has(t.id));

  return (
    <div>
      <SectionLabel>Tags</SectionLabel>

      {memberTags.length > 0 && (
        <ul className="mt-3 flex flex-wrap gap-1.5">
          {memberTags.map((t) => (
            <li key={t.id}>
              <TagChip
                t={t}
                onRemove={() => detach.mutate(t.id)}
                pending={detach.isPending}
              />
            </li>
          ))}
        </ul>
      )}

      <div className="mt-3">
        <TagPicker
          others={otherTags}
          pending={attach.isPending}
          onPick={(t) => attach.mutate(t.id)}
          onCreated={() => {
            qc.invalidateQueries({ queryKey: curationKeys.tags });
          }}
          existingNames={tags.map((t) => t.name.toLowerCase())}
        />
      </div>
    </div>
  );
}

function tagColorVars(color: TagColor): { bg: string; fg: string } {
  return {
    bg: `var(--color-tag-${color}-soft)`,
    fg: `var(--color-tag-${color})`,
  };
}

function TagChip({
  t,
  onRemove,
  pending,
}: {
  t: Tag;
  onRemove: () => void;
  pending: boolean;
}) {
  const { bg, fg } = tagColorVars(t.color);
  return (
    <button
      type="button"
      onClick={onRemove}
      disabled={pending}
      aria-label={`Remove tag ${t.name}`}
      className="group inline-flex items-center gap-1.5 px-2 py-1 text-xs rounded-[var(--radius-sm)] border disabled:opacity-60 transition-colors"
      style={{
        background: bg,
        color: fg,
        borderColor: fg,
      }}
    >
      <span>{t.name}</span>
      <span aria-hidden className="opacity-60 group-hover:opacity-100">
        ×
      </span>
    </button>
  );
}

function TagPicker({
  others,
  pending,
  onPick,
  onCreated,
  existingNames,
}: {
  others: Tag[];
  pending: boolean;
  onPick: (t: Tag) => void;
  onCreated: () => void;
  existingNames: string[];
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return others.slice(0, 50);
    return others.filter((t) => t.name.toLowerCase().includes(q)).slice(0, 50);
  }, [others, query]);

  const trimmed = query.trim();
  const exact = trimmed && existingNames.includes(trimmed.toLowerCase());
  const canCreate = trimmed.length > 0 && !exact;

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-1 px-2.5 py-1 text-[0.78rem] rounded-[var(--radius-sm)] border border-dashed border-[var(--color-rule-strong)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)] hover:border-[var(--color-ink-3)] transition-colors"
      >
        <span aria-hidden>+</span>
        <span>Add tag</span>
      </button>
      {open && (
        <div className="absolute z-20 mt-2 w-[20rem] rounded-[var(--radius-md)] bg-[var(--color-paper-3)] border border-[var(--color-rule-strong)] shadow-[0_4px_16px_rgba(0,0,0,0.06)] p-2">
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Find or create…"
            autoFocus
            maxLength={50}
            className="w-full px-2.5 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)]"
          />
          <ul role="listbox" className="mt-1.5 max-h-56 overflow-y-auto">
            {matches.map((t) => (
              <li key={t.id} className="flex items-center gap-0.5 rounded-[var(--radius-xs)] hover:bg-[var(--color-copper-soft)]">
                <button
                  type="button"
                  onClick={() => {
                    onPick(t);
                    setQuery('');
                    setOpen(false);
                  }}
                  disabled={pending}
                  className="flex-1 min-w-0 flex items-center justify-between gap-2 px-2 py-1 text-left disabled:opacity-60"
                >
                  <span className="inline-flex items-center gap-2 text-sm min-w-0">
                    <span
                      aria-hidden
                      className="w-2 h-2 rounded-full shrink-0"
                      style={{ background: `var(--color-tag-${t.color})` }}
                    />
                    <span className="text-[var(--color-ink)] truncate">
                      {t.name}
                    </span>
                  </span>
                  <span className="font-mono tabular-nums text-[0.7rem] text-[var(--color-ink-4)] shrink-0">
                    {t.listing_count}
                  </span>
                </button>
                <span className="pr-1 shrink-0">
                  <TagEditPopover
                    tag={t}
                    otherNames={existingNames.filter(
                      (n) => n !== t.name.toLowerCase(),
                    )}
                  />
                </span>
              </li>
            ))}
            {matches.length === 0 && !canCreate && (
              <li className="px-2 py-1 text-[0.78rem] text-[var(--color-ink-3)]">
                {others.length === 0
                  ? 'No tags yet — type a name to create one.'
                  : 'No matches.'}
              </li>
            )}
          </ul>
          {canCreate && (
            <CreateTagForm
              name={trimmed}
              onCreated={(t) => {
                onCreated();
                setQuery('');
                setOpen(false);
                onPick(t);
              }}
            />
          )}
        </div>
      )}
    </div>
  );
}

function CreateTagForm({
  name,
  onCreated,
}: {
  name: string;
  onCreated: (t: Tag) => void;
}) {
  const [color, setColor] = useState<TagColor>('copper');
  const [error, setError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () => createTag({ name, color }),
    onSuccess: (t) => {
      setError(null);
      onCreated(t);
    },
    onError: (err: Error) => setError(err.message || 'Failed to create'),
  });

  return (
    <div className="mt-2 pt-2 border-t border-[var(--color-rule-soft)]">
      <p className="text-[0.65rem] tracking-[0.18em] uppercase text-[var(--color-ink-4)] mb-1.5">
        Create "{name}"
      </p>
      <div className="flex items-center gap-1 flex-wrap">
        {TAG_COLORS.map((c) => (
          <button
            key={c}
            type="button"
            onClick={() => setColor(c)}
            aria-label={c}
            aria-pressed={color === c}
            className={[
              'w-5 h-5 rounded-full border transition-shadow',
              color === c ? 'ring-2 ring-offset-1 ring-offset-[var(--color-paper-3)]' : '',
            ].join(' ')}
            style={
              {
                background: `var(--color-tag-${c}-soft)`,
                borderColor: `var(--color-tag-${c})`,
                ['--tw-ring-color' as string]: `var(--color-tag-${c})`,
              } as React.CSSProperties
            }
          />
        ))}
        <button
          type="button"
          onClick={() => create.mutate()}
          disabled={create.isPending}
          className="ml-auto px-3 py-1 text-[0.75rem] rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)] disabled:opacity-50 transition-colors"
        >
          {create.isPending ? 'Creating…' : 'Create'}
        </button>
      </div>
      {error && (
        <p className="mt-1.5 text-[0.7rem] text-[var(--color-brick)]">{error}</p>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Notes                                                                      */
/* -------------------------------------------------------------------------- */

function NotesRow({
  property_id,
  sreality_id,
}: {
  property_id: number;
  sreality_id: number;
}) {
  const qc = useQueryClient();
  const [body, setBody] = useState('');
  const [error, setError] = useState<string | null>(null);

  const notesQ = useQuery({
    queryKey: curationKeys.propertyNotes(property_id),
    queryFn: () => listPropertyNotes(property_id),
    staleTime: 30_000,
  });

  const create = useMutation({
    mutationFn: (text: string) =>
      createPropertyNote(property_id, text, sreality_id),
    onSuccess: () => {
      setBody('');
      setError(null);
      qc.invalidateQueries({
        queryKey: curationKeys.propertyNotes(property_id),
      });
    },
    onError: (err: ApiError | Error) =>
      setError(err.message || 'Failed to save note'),
  });

  const notes = notesQ.data?.data ?? [];
  const trimmed = body.trim();

  return (
    <details className="group" open={notes.length > 0}>
      <summary className="cursor-pointer list-none flex items-baseline justify-between gap-4">
        <SectionLabel>
          <span>Notes</span>
          <span className="ml-2 font-mono tabular-nums text-[var(--color-ink-4)] tracking-normal">
            ({notes.length})
          </span>
        </SectionLabel>
        <span className="text-[0.7rem] tracking-wide text-[var(--color-ink-3)] group-open:hidden">
          Show
        </span>
        <span className="text-[0.7rem] tracking-wide text-[var(--color-ink-3)] hidden group-open:inline">
          Hide
        </span>
      </summary>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (trimmed.length === 0 || create.isPending) return;
          create.mutate(trimmed);
        }}
        className="mt-3"
      >
        <textarea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          placeholder="Add a note…"
          rows={2}
          maxLength={4000}
          className="w-full px-3 py-2 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)] resize-y"
        />
        <div className="mt-2 flex items-center justify-between gap-3">
          <p className="text-[0.7rem] text-[var(--color-ink-4)] tabular-nums">
            {body.length} / 4000
          </p>
          <button
            type="submit"
            disabled={trimmed.length === 0 || create.isPending}
            className="px-3 py-1 text-[0.78rem] rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            {create.isPending ? 'Saving…' : 'Save note'}
          </button>
        </div>
        {error && (
          <p className="mt-1.5 text-[0.7rem] text-[var(--color-brick)]">{error}</p>
        )}
      </form>

      {notesQ.isLoading ? (
        <p className="mt-3 text-sm text-[var(--color-ink-3)]">Loading…</p>
      ) : notes.length === 0 ? null : (
        <ul className="mt-4 space-y-3">
          {notes.map((n) => (
            <li key={n.id}>
              <NoteRow note={n} />
            </li>
          ))}
        </ul>
      )}
    </details>
  );
}

function NoteRow({ note }: { note: Note }) {
  return (
    <div className="border-l-2 border-[var(--color-copper)]/40 pl-3">
      <p className="text-sm text-[var(--color-ink)] whitespace-pre-wrap break-words">
        {note.body}
      </p>
      <p
        className="mt-1 text-[0.7rem] tracking-wide text-[var(--color-ink-4)] cursor-help"
        title={fmtAbsolute(note.created_at)}
      >
        {fmtRelative(note.created_at)}
      </p>
    </div>
  );
}
