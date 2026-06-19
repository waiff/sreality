/* Saved filter presets, surfaced as buttons next to the Browse headline.
 *
 * A preset is a named filter set + sort order. Clicking a chip restores both
 * (Browse owns the URL write via `onLoad`); the active chip is highlighted and,
 * once the operator edits a filter or the sort, an "Update" button appears.
 * Save / edit / delete go through the bearer-gated FastAPI service
 * (migration 151) — a preset never fires a notification, unlike a Watchdog.
 * Each preset can carry a colour from the shared tag palette (migration 201);
 * the chip then renders in that colour (copper/neutral when uncolored).
 *
 * The chips are drag-reorderable (each carries a grip handle). Order is
 * operator-controlled and server-persisted via `position` (migration 198):
 * a drag optimistically rewrites the cached order, then PUTs the full id-list
 * to /filter-presets/reorder, rolling back to the server's truth on error.
 *
 * The whole bar hides when the API base URL isn't configured (presets need the
 * service to read or write). */

import { useEffect, useRef, useState, type CSSProperties } from 'react';
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
  rectSortingStrategy,
  sortableKeyboardCoordinates,
  useSortable,
} from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';

import {
  ApiError,
  createFilterPreset,
  deleteFilterPreset,
  isApiConfigured,
  listFilterPresets,
  reorderFilterPresets,
  updateFilterPreset,
} from '@/lib/api';
import {
  filterPresetKeys,
  DEFAULT_SORT,
  sortToParam,
  type SortSpec,
} from '@/lib/queries';
import {
  filtersEqualForPreset,
  filtersForPreset,
  readPresetSpec,
  type ListingFilters,
  type PresetSpec,
} from '@/lib/filters';
import type { FilterPreset, TagColor } from '@/lib/types';
import PresetSaveModal from '@/components/PresetSaveModal';

export interface PresetBarProps {
  filters: ListingFilters;
  sort: SortSpec;
  activePresetId: string | null;
  onLoad: (preset: FilterPreset) => void;
  onActivePresetIdChange: (id: string | null) => void;
}

type ModalState =
  | { mode: 'save' }
  | { mode: 'update'; preset: FilterPreset }
  | { mode: 'edit'; preset: FilterPreset };

type PresetsResponse = { data: FilterPreset[]; total: number };

const chipBase =
  'inline-flex items-center rounded-[var(--radius-sm)] border text-[0.8rem] transition-colors';

export default function PresetBar({
  filters,
  sort,
  activePresetId,
  onLoad,
  onActivePresetIdChange,
}: PresetBarProps) {
  const enabled = isApiConfigured();
  const qc = useQueryClient();
  const barRef = useRef<HTMLDivElement>(null);

  const presetsQ = useQuery({
    queryKey: filterPresetKeys.all,
    queryFn: listFilterPresets,
    enabled,
  });
  const presets = presetsQ.data?.data ?? [];

  const sortParam = sortToParam(sort);
  const defaultSortParam = sortToParam(DEFAULT_SORT);

  const active =
    activePresetId != null
      ? presets.find((p) => p.id === activePresetId) ?? null
      : null;
  const activeSpec = active ? readPresetSpec(active.filter_spec) : null;
  /* Dirty when either the filters OR the sort drift from the loaded preset. */
  const dirty = activeSpec
    ? !filtersEqualForPreset(filters, activeSpec.filters) ||
      (activeSpec.sort ?? defaultSortParam) !== sortParam
    : false;
  const hasMapArea = filters.bounds != null;

  const [modal, setModal] = useState<ModalState | null>(null);
  const [menuId, setMenuId] = useState<string | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

  const closeMenu = () => {
    setMenuId(null);
    setConfirmDeleteId(null);
  };

  /* Click-away closes the per-chip kebab menu. */
  useEffect(() => {
    if (menuId == null) return;
    const onDown = (e: MouseEvent) => {
      if (barRef.current && !barRef.current.contains(e.target as Node)) {
        setMenuId(null);
        setConfirmDeleteId(null);
      }
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [menuId]);

  const invalidate = () => qc.invalidateQueries({ queryKey: filterPresetKeys.all });

  const createMut = useMutation({
    mutationFn: (input: { name: string; filter_spec: PresetSpec; color: TagColor | null }) =>
      createFilterPreset(input),
    onSuccess: (created) => {
      invalidate();
      onActivePresetIdChange(created.id);
      setModal(null);
    },
  });

  const updateMut = useMutation({
    mutationFn: (input: {
      id: string;
      name?: string;
      filter_spec?: PresetSpec;
      color?: TagColor | null;
    }) =>
      updateFilterPreset(input.id, {
        name: input.name,
        filter_spec: input.filter_spec,
        color: input.color,
      }),
    onSuccess: () => {
      invalidate();
      setModal(null);
    },
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => deleteFilterPreset(id),
    onSuccess: (_res, id) => {
      invalidate();
      if (id === activePresetId) onActivePresetIdChange(null);
      closeMenu();
    },
  });

  /* Persist a new order. The drag already wrote the optimistic order into the
   * cache; adopt the server's canonical list on success, roll back on error. */
  const reorderMut = useMutation({
    mutationFn: (ids: string[]) => reorderFilterPresets(ids),
    onSuccess: (res) => qc.setQueryData(filterPresetKeys.all, res),
    onError: invalidate,
  });

  const sensors = useSensors(
    /* A small activation distance keeps a click on the grip from registering
     * as a drag; the keyboard sensor makes reordering accessible. */
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const handleDragEnd = (event: DragEndEvent) => {
    const { active: dragged, over } = event;
    if (!over || dragged.id === over.id) return;
    const oldIndex = presets.findIndex((p) => p.id === dragged.id);
    const newIndex = presets.findIndex((p) => p.id === over.id);
    if (oldIndex < 0 || newIndex < 0) return;
    const reordered = arrayMove(presets, oldIndex, newIndex);
    qc.setQueryData<PresetsResponse>(filterPresetKeys.all, (prev) =>
      prev ? { ...prev, data: reordered } : prev,
    );
    reorderMut.mutate(reordered.map((p) => p.id));
  };

  if (!enabled) return null;

  const errMsg = (e: unknown): string | null =>
    e instanceof ApiError ? e.message : e ? 'Something went wrong.' : null;

  const handleSubmit = (
    name: string,
    includeMapArea: boolean,
    color: TagColor | null,
  ) => {
    if (modal == null) return;
    // Save / Update capture the current filters AND the current sort; Edit only
    // touches metadata (name + colour), leaving the stored filters intact.
    const spec: PresetSpec = {
      filters: filtersForPreset(filters, includeMapArea),
      sort: sortParam,
    };
    if (modal.mode === 'save') {
      createMut.mutate({ name, filter_spec: spec, color });
    } else if (modal.mode === 'update') {
      updateMut.mutate({ id: modal.preset.id, name, filter_spec: spec, color });
    } else {
      updateMut.mutate({ id: modal.preset.id, name, color });
    }
  };

  const reorderable = presets.length > 1;

  return (
    <div ref={barRef} className="flex items-center gap-2 flex-wrap">
      {presets.length === 0 && !presetsQ.isLoading ? (
        <span className="text-[0.75rem] text-[var(--color-ink-4)]">
          No saved presets yet.
        </span>
      ) : null}

      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragStart={closeMenu}
        onDragEnd={handleDragEnd}
      >
        <SortableContext
          items={presets.map((p) => p.id)}
          strategy={rectSortingStrategy}
        >
          {presets.map((p) => (
            <SortablePresetChip
              key={p.id}
              preset={p}
              reorderable={reorderable}
              isActive={p.id === activePresetId}
              dirty={dirty}
              menuOpen={menuId === p.id}
              confirmingDelete={confirmDeleteId === p.id}
              deleting={deleteMut.isPending}
              onLoad={onLoad}
              onToggleMenu={(id) => {
                setConfirmDeleteId(null);
                setMenuId((cur) => (cur === id ? null : id));
              }}
              onEdit={(preset) => {
                setModal({ mode: 'edit', preset });
                closeMenu();
              }}
              onAskDelete={(id) => setConfirmDeleteId(id)}
              onConfirmDelete={(id) => deleteMut.mutate(id)}
            />
          ))}
        </SortableContext>
      </DndContext>

      {active && dirty ? (
        <button
          type="button"
          onClick={() => setModal({ mode: 'update', preset: active })}
          className="px-2.5 py-1 text-[0.8rem] rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)] transition-colors"
          title="Save the current filter changes back into this preset"
        >
          Update preset
        </button>
      ) : null}

      <button
        type="button"
        onClick={() => setModal({ mode: 'save' })}
        className="px-2.5 py-1 text-[0.8rem] rounded-[var(--radius-sm)] border border-dashed border-[var(--color-rule-strong)] text-[var(--color-ink-3)] hover:text-[var(--color-ink)] hover:border-[var(--color-copper)] transition-colors"
        title="Save the current filters as a new preset"
      >
        + Save preset
      </button>

      {modal ? (
        <PresetSaveModal
          title={
            modal.mode === 'save'
              ? 'Save filters as a preset'
              : modal.mode === 'update'
                ? 'Update preset'
                : 'Edit preset'
          }
          initialName={modal.mode === 'save' ? '' : modal.preset.name}
          initialColor={modal.mode === 'save' ? null : modal.preset.color}
          submitLabel={
            modal.mode === 'save'
              ? 'Save preset'
              : modal.mode === 'update'
                ? 'Update preset'
                : 'Save'
          }
          showMapAreaToggle={modal.mode !== 'edit' && hasMapArea}
          initialIncludeMapArea={
            modal.mode === 'update'
              ? readPresetSpec(modal.preset.filter_spec).filters.bounds != null
              : false
          }
          busy={createMut.isPending || updateMut.isPending}
          error={errMsg(createMut.error) ?? errMsg(updateMut.error)}
          onSubmit={handleSubmit}
          onClose={() => setModal(null)}
        />
      ) : null}
    </div>
  );
}

interface SortablePresetChipProps {
  preset: FilterPreset;
  reorderable: boolean;
  isActive: boolean;
  dirty: boolean;
  menuOpen: boolean;
  confirmingDelete: boolean;
  deleting: boolean;
  onLoad: (preset: FilterPreset) => void;
  onToggleMenu: (id: string) => void;
  onEdit: (preset: FilterPreset) => void;
  onAskDelete: (id: string) => void;
  onConfirmDelete: (id: string) => void;
}

function SortablePresetChip({
  preset,
  reorderable,
  isActive,
  dirty,
  menuOpen,
  confirmingDelete,
  deleting,
  onLoad,
  onToggleMenu,
  onEdit,
  onAskDelete,
  onConfirmDelete,
}: SortablePresetChipProps) {
  const {
    attributes,
    listeners,
    setNodeRef,
    setActivatorNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: preset.id, disabled: !reorderable });

  /* The chip's accent comes from its colour (shared tag palette) or copper when
   * uncolored — exposed as CSS vars so active/hover states reuse one source.
   * Uncolored chips render exactly as before (copper active, neutral inactive). */
  const accent = preset.color
    ? `var(--color-tag-${preset.color})`
    : 'var(--color-copper)';
  const accentSoft = preset.color
    ? `var(--color-tag-${preset.color}-soft)`
    : 'var(--color-copper-soft)';

  const style: CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
    zIndex: isDragging ? 30 : undefined,
    ['--preset-accent' as string]: accent,
    ['--preset-accent-soft' as string]: accentSoft,
  };

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`relative ${isDragging ? 'opacity-90' : ''}`}
    >
      <div
        className={[
          chipBase,
          isDragging ? 'shadow-md' : '',
          isActive
            ? 'border-[var(--preset-accent)] bg-[var(--preset-accent-soft)] text-[var(--preset-accent)]'
            : preset.color
              ? 'border-[var(--preset-accent)] text-[var(--preset-accent)] hover:bg-[var(--preset-accent-soft)]'
              : 'border-[var(--color-rule)] text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)] hover:text-[var(--color-ink)]',
        ].join(' ')}
      >
        {reorderable ? (
          <button
            type="button"
            ref={setActivatorNodeRef}
            {...attributes}
            {...listeners}
            className="flex items-center self-stretch px-1.5 cursor-grab touch-none text-[var(--color-ink-4)] hover:text-[var(--color-ink-2)] active:cursor-grabbing"
            aria-label={`Reorder preset: ${preset.name}`}
            title="Drag to reorder"
          >
            <GripIcon />
          </button>
        ) : null}
        <button
          type="button"
          onClick={() => onLoad(preset)}
          className={`${reorderable ? 'pl-0.5' : 'pl-2.5'} pr-2.5 py-1 max-w-[16rem] truncate`}
          title={`Load preset: ${preset.name}`}
        >
          {preset.name}
          {isActive && dirty ? (
            <span
              className="ml-1 text-[var(--preset-accent)]"
              title="Edited since loaded — use Update to save changes"
              aria-label="edited"
            >
              •
            </span>
          ) : null}
        </button>
        <button
          type="button"
          onClick={() => onToggleMenu(preset.id)}
          className="px-1.5 py-1 border-l border-[var(--color-rule)] opacity-60 hover:opacity-100"
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          title="Preset options"
        >
          ⋯
        </button>
      </div>

      {menuOpen ? (
        <div
          role="menu"
          className="absolute left-0 top-[calc(100%+4px)] z-30 min-w-[10rem] rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)] py-1 shadow-lg"
        >
          <button
            type="button"
            role="menuitem"
            onClick={() => onEdit(preset)}
            className="block w-full px-3 py-1.5 text-left text-[0.8rem] text-[var(--color-ink-2)] hover:bg-[var(--color-paper-2)] hover:text-[var(--color-ink)]"
          >
            Edit…
          </button>
          {confirmingDelete ? (
            <button
              type="button"
              role="menuitem"
              onClick={() => onConfirmDelete(preset.id)}
              disabled={deleting}
              className="block w-full px-3 py-1.5 text-left text-[0.8rem] text-[var(--color-brick)] hover:bg-[var(--color-brick-soft)] disabled:opacity-50"
            >
              {deleting ? 'Deleting…' : 'Confirm delete'}
            </button>
          ) : (
            <button
              type="button"
              role="menuitem"
              onClick={() => onAskDelete(preset.id)}
              className="block w-full px-3 py-1.5 text-left text-[0.8rem] text-[var(--color-ink-2)] hover:bg-[var(--color-paper-2)] hover:text-[var(--color-brick)]"
            >
              Delete…
            </button>
          )}
        </div>
      ) : null}
    </div>
  );
}

/* Six-dot grip — the standard "drag me" affordance, dim at rest and
 * brightening on hover (mirrors the ResizeHandle idiom). */
function GripIcon() {
  return (
    <svg width="9" height="14" viewBox="0 0 9 14" fill="currentColor" aria-hidden>
      <circle cx="2" cy="3" r="1.1" />
      <circle cx="7" cy="3" r="1.1" />
      <circle cx="2" cy="7" r="1.1" />
      <circle cx="7" cy="7" r="1.1" />
      <circle cx="2" cy="11" r="1.1" />
      <circle cx="7" cy="11" r="1.1" />
    </svg>
  );
}
