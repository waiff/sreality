import { useEffect, useState, type CSSProperties } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from '@dnd-kit/core';
import {
  SortableContext,
  arrayMove,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';

import {
  getDedupTagPriorities,
  updateDedupTagPriority,
  type DedupTagPriority,
} from '@/lib/api';
import { imageTagLabel } from '@/lib/imageTags';

/* Per-family comparison-tag priority editor. The dedup visual layer compares like
 * rooms in this order and stops at the first High verdict, so the tag at the TOP
 * leads — facade for houses, the site plan for plots, wet rooms for flats. The
 * operator reorders by dragging; the backend completes any omission from the coded
 * default, so a list can never silently drop a room. Civic-archive system: bordered
 * ledger rows, ochre "edited" marker, copper drag affordance. */

const FAMILY_LABELS: Record<string, string> = {
  byt: 'Byty',
  dum: 'Domy',
  komercni: 'Komerční',
  ostatni: 'Ostatní',
  pozemek: 'Pozemky',
};

export default function DedupTagPrioritiesSection() {
  const q = useQuery({
    queryKey: ['dedup-tag-priorities'],
    queryFn: getDedupTagPriorities,
  });

  if (q.isLoading)
    return <p className="text-sm text-[var(--color-ink-3)]">Loading…</p>;
  if (q.isError)
    return (
      <p className="text-sm text-[var(--color-brick)]">
        Couldn’t load the tag priorities.
      </p>
    );

  return (
    <div className="space-y-6">
      <p className="text-[0.78rem] leading-snug text-[var(--color-ink-3)] max-w-prose">
        The dedup visual layer compares matching rooms in this order and stops at the
        first confident match — so the tag at the top leads. Drag to reorder per listing
        type. Defaults are grounded (facade for houses, situation plan for plots, wet
        rooms for flats); reordering only changes which photos are compared first.
      </p>
      {(q.data?.data ?? []).map((fam) => (
        <FamilyList key={fam.family} fam={fam} />
      ))}
    </div>
  );
}

function FamilyList({ fam }: { fam: DedupTagPriority }) {
  const qc = useQueryClient();
  // Local order so a drag feels instant; re-synced whenever the server value changes.
  const [order, setOrder] = useState<string[]>(fam.order);
  useEffect(() => setOrder(fam.order), [fam.order]);

  const mut = useMutation({
    mutationFn: (next: string[]) => updateDedupTagPriority(fam.family, next),
    // onSettled (not onError): a refetch re-syncs the local order to the server on BOTH
    // success and failure — so a failed write reverts the optimistic drag AND surfaces via
    // the app-wide MutationCache error toast (which is skipped for mutations with their own
    // onError). No silent failures.
    onSettled: () => qc.invalidateQueries({ queryKey: ['dedup-tag-priorities'] }),
  });

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const handleDragEnd = (e: DragEndEvent) => {
    const { active, over } = e;
    if (!over || active.id === over.id) return;
    const from = order.indexOf(String(active.id));
    const to = order.indexOf(String(over.id));
    if (from < 0 || to < 0) return;
    const next = arrayMove(order, from, to);
    setOrder(next);
    mut.mutate(next);
  };

  const isDefault =
    order.length === fam.default_order.length &&
    order.every((t, i) => t === fam.default_order[i]);

  return (
    <div>
      <div className="flex items-center gap-2 mb-1.5">
        <span className="text-xs tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          {FAMILY_LABELS[fam.family] ?? fam.family}
        </span>
        {!isDefault && (
          <>
            <span
              title="Changed from the default"
              className="inline-flex items-center gap-1 text-[0.62rem] uppercase tracking-[0.1em] text-[var(--color-ochre)]"
            >
              <span className="w-1.5 h-1.5 rounded-full bg-[var(--color-ochre)]" />
              edited
            </span>
            <button
              type="button"
              disabled={mut.isPending}
              onClick={() => {
                setOrder(fam.default_order);
                mut.mutate(fam.default_order);
              }}
              className="text-[0.7rem] text-[var(--color-ink-3)] hover:text-[var(--color-copper)] underline decoration-dotted underline-offset-2 disabled:opacity-50"
            >
              Reset to default
            </button>
          </>
        )}
      </div>
      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragEnd={handleDragEnd}
      >
        <SortableContext items={order} strategy={verticalListSortingStrategy}>
          <ol className="border border-[var(--color-rule)] rounded-[var(--radius-sm)] divide-y divide-[var(--color-rule)] bg-[var(--color-paper)]">
            {order.map((tag, i) => (
              <SortableTag key={tag} tag={tag} index={i} disabled={mut.isPending} />
            ))}
          </ol>
        </SortableContext>
      </DndContext>
    </div>
  );
}

function SortableTag({
  tag,
  index,
  disabled,
}: {
  tag: string;
  index: number;
  disabled: boolean;
}) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: tag, disabled });

  const style: CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
    zIndex: isDragging ? 30 : undefined,
  };

  return (
    <li
      ref={setNodeRef}
      style={style}
      className={[
        'flex items-center gap-3 px-3 py-2 bg-[var(--color-paper)]',
        isDragging ? 'shadow-md opacity-95' : '',
      ].join(' ')}
    >
      <button
        type="button"
        aria-label="Drag to reorder"
        disabled={disabled}
        {...attributes}
        {...listeners}
        className="cursor-grab active:cursor-grabbing text-[var(--color-ink-4)] hover:text-[var(--color-copper)] disabled:opacity-50 touch-none"
      >
        {/* two-row grip glyph (inline SVG — no icon dep) */}
        <svg width="12" height="16" viewBox="0 0 12 16" aria-hidden="true">
          <g fill="currentColor">
            <circle cx="3" cy="4" r="1.4" />
            <circle cx="9" cy="4" r="1.4" />
            <circle cx="3" cy="8" r="1.4" />
            <circle cx="9" cy="8" r="1.4" />
            <circle cx="3" cy="12" r="1.4" />
            <circle cx="9" cy="12" r="1.4" />
          </g>
        </svg>
      </button>
      <span className="w-5 text-right text-[0.72rem] font-mono tabular-nums text-[var(--color-ink-4)]">
        {index + 1}
      </span>
      <span className="text-sm text-[var(--color-ink)]">
        {imageTagLabel(tag) ?? tag}
      </span>
    </li>
  );
}
