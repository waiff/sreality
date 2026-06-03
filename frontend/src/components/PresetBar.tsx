/* Saved filter presets, surfaced as buttons next to the Browse headline.
 *
 * A preset is a named filter set. Clicking a chip restores its filters (Browse
 * owns the URL write via `onLoad`); the active chip is highlighted and, once
 * the operator edits a filter, an "Update" button appears. Save / rename /
 * delete go through the bearer-gated FastAPI service (migration 150) — a preset
 * never fires a notification, unlike a Watchdog.
 *
 * The whole bar hides when the API base URL isn't configured (presets need the
 * service to read or write). */

import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  ApiError,
  createFilterPreset,
  deleteFilterPreset,
  isApiConfigured,
  listFilterPresets,
  updateFilterPreset,
} from '@/lib/api';
import { filterPresetKeys } from '@/lib/queries';
import {
  filtersEqualForPreset,
  filtersForPreset,
  type ListingFilters,
} from '@/lib/filters';
import type { FilterPreset } from '@/lib/types';
import PresetSaveModal from '@/components/PresetSaveModal';

export interface PresetBarProps {
  filters: ListingFilters;
  activePresetId: string | null;
  onLoad: (preset: FilterPreset) => void;
  onActivePresetIdChange: (id: string | null) => void;
}

type ModalState =
  | { mode: 'save' }
  | { mode: 'update'; preset: FilterPreset }
  | { mode: 'rename'; preset: FilterPreset };

export default function PresetBar({
  filters,
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

  const active =
    activePresetId != null
      ? presets.find((p) => p.id === activePresetId) ?? null
      : null;
  const dirty = active ? !filtersEqualForPreset(filters, active.filter_spec) : false;
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
    mutationFn: (input: { name: string; filter_spec: ListingFilters }) =>
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
      filter_spec?: ListingFilters;
    }) => updateFilterPreset(input.id, { name: input.name, filter_spec: input.filter_spec }),
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

  if (!enabled) return null;

  const errMsg = (e: unknown): string | null =>
    e instanceof ApiError ? e.message : e ? 'Something went wrong.' : null;

  const handleSubmit = (name: string, includeMapArea: boolean) => {
    if (modal == null) return;
    if (modal.mode === 'save') {
      createMut.mutate({ name, filter_spec: filtersForPreset(filters, includeMapArea) });
    } else if (modal.mode === 'update') {
      updateMut.mutate({
        id: modal.preset.id,
        name,
        filter_spec: filtersForPreset(filters, includeMapArea),
      });
    } else {
      updateMut.mutate({ id: modal.preset.id, name });
    }
  };

  const chipBase =
    'inline-flex items-center rounded-[var(--radius-sm)] border text-[0.8rem] transition-colors';

  return (
    <div ref={barRef} className="flex items-center gap-2 flex-wrap">
      {presets.length === 0 && !presetsQ.isLoading ? (
        <span className="text-[0.75rem] text-[var(--color-ink-4)]">
          No saved presets yet.
        </span>
      ) : null}

      {presets.map((p) => {
        const isActive = p.id === activePresetId;
        return (
          <div key={p.id} className="relative">
            <div
              className={[
                chipBase,
                isActive
                  ? 'border-[var(--color-copper)] bg-[var(--color-copper-soft)] text-[var(--color-copper)]'
                  : 'border-[var(--color-rule)] text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)] hover:text-[var(--color-ink)]',
              ].join(' ')}
            >
              <button
                type="button"
                onClick={() => onLoad(p)}
                className="px-2.5 py-1 max-w-[16rem] truncate"
                title={`Load preset: ${p.name}`}
              >
                {p.name}
                {isActive && dirty ? (
                  <span
                    className="ml-1 text-[var(--color-copper)]"
                    title="Edited since loaded — use Update to save changes"
                    aria-label="edited"
                  >
                    •
                  </span>
                ) : null}
              </button>
              <button
                type="button"
                onClick={() => {
                  setConfirmDeleteId(null);
                  setMenuId((cur) => (cur === p.id ? null : p.id));
                }}
                className="px-1.5 py-1 border-l border-[var(--color-rule)] opacity-60 hover:opacity-100"
                aria-haspopup="menu"
                aria-expanded={menuId === p.id}
                title="Preset options"
              >
                ⋯
              </button>
            </div>

            {menuId === p.id ? (
              <div
                role="menu"
                className="absolute left-0 top-[calc(100%+4px)] z-30 min-w-[10rem] rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)] py-1 shadow-lg"
              >
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    setModal({ mode: 'rename', preset: p });
                    closeMenu();
                  }}
                  className="block w-full px-3 py-1.5 text-left text-[0.8rem] text-[var(--color-ink-2)] hover:bg-[var(--color-paper-2)] hover:text-[var(--color-ink)]"
                >
                  Rename…
                </button>
                {confirmDeleteId === p.id ? (
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => deleteMut.mutate(p.id)}
                    disabled={deleteMut.isPending}
                    className="block w-full px-3 py-1.5 text-left text-[0.8rem] text-[var(--color-brick)] hover:bg-[var(--color-brick-soft)] disabled:opacity-50"
                  >
                    {deleteMut.isPending ? 'Deleting…' : 'Confirm delete'}
                  </button>
                ) : (
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => setConfirmDeleteId(p.id)}
                    className="block w-full px-3 py-1.5 text-left text-[0.8rem] text-[var(--color-ink-2)] hover:bg-[var(--color-paper-2)] hover:text-[var(--color-brick)]"
                  >
                    Delete…
                  </button>
                )}
              </div>
            ) : null}
          </div>
        );
      })}

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
                : 'Rename preset'
          }
          initialName={modal.mode === 'save' ? '' : modal.preset.name}
          submitLabel={
            modal.mode === 'save'
              ? 'Save preset'
              : modal.mode === 'update'
                ? 'Update preset'
                : 'Rename'
          }
          showMapAreaToggle={modal.mode !== 'rename' && hasMapArea}
          initialIncludeMapArea={
            modal.mode === 'update' ? modal.preset.filter_spec.bounds != null : false
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
