import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  DndContext,
  DragOverlay,
  KeyboardSensor,
  PointerSensor,
  useDroppable,
  useSensor,
  useSensors,
  type DragEndEvent,
  type DragStartEvent,
} from '@dnd-kit/core';
import {
  archivePipelineStage,
  createPipelineStage,
  movePipelineCard,
  removePipelineCard,
  reorderPipelineStages,
  updatePipelineStage,
} from '@/lib/api';
import {
  fetchPipelineBoard,
  fetchPipelineStages,
  matchesDistricts,
  pipelineKeys,
} from '@/lib/queries';
import {
  cachedStage,
  dropCard,
  NO_ROLLBACK,
  placeCard,
  revalidatePipeline,
} from '@/lib/pipelineCache';
import { CardHydrationProvider } from '@/lib/hydration';
import { LocationTypeahead } from '@/components/filter-controls/LocationTypeahead';
import { type ListingStatus } from '@/lib/filters';
import { FILTER_REGISTRY } from '@/lib/filterRegistry.generated';
import TagColorPicker from '@/components/TagColorPicker';
import { FunnelIcon, InfoIcon } from '@/components/icons';
import BoardCard, {
  CardFace,
  CARD_PREFIX,
  STAGE_PREFIX,
} from '@/components/pipeline/BoardCard';
import { sortParamOf } from '@/lib/cardSort';
import { PIPELINE_SORT_OPTIONS, sortPipelineCards } from '@/lib/pipelineSort';
import { usePipelineViewState } from '@/lib/pipelineState';
import { useCityQuality, type CityQualityByObec } from '@/lib/useCityQuality';
import {
  type PipelineBoardCard,
  type PipelineStage,
  type TagColor,
} from '@/lib/types';

/* Property-type (category_main) options for the pipeline filter — the SAME
 * canonical source as Browse's TYPE tabs (the generated filter registry), so the
 * Byty/Domy/Komerční/… labels never drift from one hardcode to another. */
const CATEGORY_MAIN_ENUM =
  FILTER_REGISTRY.filters.find((f) => f.id === 'category_main')?.enum_values ?? [];
const CATEGORY_MAIN_ORDER: string[] = CATEGORY_MAIN_ENUM.map((o) => String(o.value));
const CATEGORY_MAIN_LABEL: Record<string, string> = Object.fromEntries(
  CATEGORY_MAIN_ENUM.map((o) => [String(o.value), o.label_cs]),
);

/* Active/inactive filter — the SAME any/active/inactive vocabulary and Czech
 * labels as Browse's `status` filter (single source of truth: the registry),
 * applied here against the property-grain `is_active` rollup (bool_or over
 * child listings, rule #15/#20) that fetchPipelineBoard already selects from
 * properties_public. */
const STATUS_ENUM =
  FILTER_REGISTRY.filters.find((f) => f.id === 'status')?.enum_values ?? [];
const STATUS_ORDER: ListingStatus[] = STATUS_ENUM.map(
  (o) => String(o.value) as ListingStatus,
);
const STATUS_LABEL: Record<string, string> = Object.fromEntries(
  STATUS_ENUM.map((o) => [String(o.value), o.label_cs]),
);

export default function Pipeline() {
  const [manage, setManage] = useState(false);
  /* Filters AND sort live in the URL (lib/pipelineState) using Browse's own
   * param vocabulary, so a filtered/sorted board is linkable and survives a
   * reload. `manage` stays local — it's a transient editor toggle, not a view. */
  const {
    status,
    types,
    districts,
    sort,
    setStatus,
    toggleType,
    clearTypes,
    setDistricts,
    setSort,
  } = usePipelineViewState();
  const stagesQ = useQuery({
    queryKey: pipelineKeys.stages,
    queryFn: fetchPipelineStages,
    staleTime: 60_000,
  });
  const boardQ = useQuery({
    queryKey: pipelineKeys.board,
    queryFn: fetchPipelineBoard,
    staleTime: 30_000,
  });

  const stages = stagesQ.data ?? [];
  const cards = boardQ.data ?? [];

  // Property types actually present in the pipeline (registry order) — the chip
  // set. Depends on the stable query reference (not the per-render `cards`).
  const presentTypes = useMemo(() => {
    const set = new Set<string>();
    for (const c of boardQ.data ?? []) if (c.category_main) set.add(c.category_main);
    return CATEGORY_MAIN_ORDER.filter((t) => set.has(t));
  }, [boardQ.data]);

  // Only offer the status filter when the board actually holds a delisted
  // property to filter out — an all-active pipeline has nothing to stratify.
  const hasInactive = useMemo(
    () => (boardQ.data ?? []).some((c) => !c.is_active),
    [boardQ.data],
  );

  // Client-side filters (the board is small, rule #22): type chips + the region
  // picker + active/inactive status, applied in-memory. Region reuses Browse's
  // exact chip semantics via matchesDistricts; status reuses Browse's
  // any/active/inactive vocabulary against the same is_active rollup. Empty /
  // 'any' = no constraint.
  const filtersActive =
    types.size > 0 || districts.length > 0 || status !== 'any';
  const filteredCards = useMemo(() => {
    let result = boardQ.data ?? [];
    if (types.size > 0) {
      result = result.filter(
        (c) => c.category_main != null && types.has(c.category_main),
      );
    }
    if (districts.length > 0) {
      result = result.filter((c) => matchesDistricts(c, districts));
    }
    if (status !== 'any') {
      result = result.filter((c) => c.is_active === (status === 'active'));
    }
    return result;
  }, [boardQ.data, types, districts, status]);

  /* The decoration cohort: the representative listing of every card ON the
   * board (not just the filtered view — filtering is client-side and instant,
   * so hydrating the full board once keeps a filter toggle free instead of
   * re-keying the enrichment query on every chip click). */
  const visibleListingIds = useMemo(
    () =>
      (boardQ.data ?? [])
        .map((c) => c.listing_id)
        .filter((id): id is number => id != null),
    [boardQ.data],
  );

  /* Curated-city indexes for the card strip. Cached forever and keyed shared
   * with the Browse map, so this is free once either surface has loaded them;
   * `enabled` only once there is a board to decorate. */
  const { byObec: cityQuality } = useCityQuality(cards.length > 0);

  const byStage = useMemo(() => {
    const m = new Map<number, PipelineBoardCard[]>();
    for (const s of stagesQ.data ?? []) m.set(s.id, []);
    for (const c of filteredCards) {
      const bucket = m.get(c.stage_id);
      if (bucket) bucket.push(c);
    }
    /* Sort WITHIN each column, not across the board — a kanban's vertical axis
     * is per-column. Every comparator tiebreaks on property_id, so equal keys
     * (and colliding board_positions, which live data has) hold a stable order
     * across refetches instead of reshuffling. */
    for (const [id, bucket] of m) m.set(id, sortPipelineCards(bucket, sort));
    return m;
  }, [stagesQ.data, filteredCards, sort]);

  return (
    <div className="px-6 py-8">
      <header className="flex items-baseline justify-between">
        <div>
          <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
            Pipeline
          </p>
          <h1
            className="mt-1.5 text-[2.4rem] leading-[1.05]"
            style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
          >
            Pipeline obchodů
          </h1>
        </div>
        <div className="flex items-center gap-4">
          <p className="text-[0.75rem] tracking-wide text-[var(--color-ink-3)] font-mono tabular-nums">
            {/* An em dash while the count is genuinely unknown. Rendering 0
                during the load states "your pipeline is empty", which is a
                claim, not a placeholder — and it was briefly true on every
                visit before the board resolved. */}
            {boardQ.data === undefined
              ? '—'
              : filtersActive
                ? `${filteredCards.length} z ${cards.length}`
                : cards.length}{' '}
            nemovitostí
          </p>
          <button
            type="button"
            onClick={() => setManage((v) => !v)}
            aria-pressed={manage}
            className="text-[0.72rem] tracking-[0.1em] uppercase px-2.5 py-1 rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)] hover:text-[var(--color-ink)]"
          >
            {manage ? 'Hotovo' : 'Spravovat fáze'}
          </button>
        </div>
      </header>

      {manage && stages.length > 0 && <StageManager stages={stages} />}

      {/* Filters — active/inactive status (only when the pipeline holds a
          delisted property) + property type (only when the pipeline holds >1
          type) + the region picker. The region control is the SAME
          LocationTypeahead Browse and Datasets use; the status pills are the
          SAME any/active/inactive vocabulary as Browse's status filter
          (single source of truth: FILTER_REGISTRY). All three apply
          client-side (rule #22, the board is small). */}
      {cards.length > 0 && (
        <div className="mt-5 flex flex-col gap-3">
          {hasInactive && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="mr-1 text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
                Stav
              </span>
              {STATUS_ORDER.map((s) => {
                const active = status === s;
                return (
                  <button
                    key={s}
                    type="button"
                    aria-pressed={active}
                    onClick={() => setStatus(active ? 'any' : s)}
                    className={[
                      'rounded-[var(--radius-sm)] border px-2.5 py-1 text-[0.78rem] transition-colors',
                      active
                        ? 'border-[var(--color-copper)] bg-[var(--color-copper-soft)] text-[var(--color-copper)]'
                        : 'border-[var(--color-rule)] text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)] hover:text-[var(--color-ink)]',
                    ].join(' ')}
                  >
                    {STATUS_LABEL[s] ?? s}
                  </button>
                );
              })}
            </div>
          )}
          {presentTypes.length >= 2 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="mr-1 text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
                Typ
              </span>
              {presentTypes.map((t) => {
                const active = types.has(t);
                return (
                  <button
                    key={t}
                    type="button"
                    aria-pressed={active}
                    onClick={() => toggleType(t)}
                    className={[
                      'rounded-[var(--radius-sm)] border px-2.5 py-1 text-[0.78rem] transition-colors',
                      active
                        ? 'border-[var(--color-copper)] bg-[var(--color-copper-soft)] text-[var(--color-copper)]'
                        : 'border-[var(--color-rule)] text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)] hover:text-[var(--color-ink)]',
                    ].join(' ')}
                  >
                    {CATEGORY_MAIN_LABEL[t] ?? t}
                  </button>
                );
              })}
              {types.size > 0 && (
                <button
                  type="button"
                  onClick={clearTypes}
                  className="ml-1 text-[0.72rem] text-[var(--color-ink-3)] underline underline-offset-2 hover:text-[var(--color-ink)]"
                >
                  Vše
                </button>
              )}
            </div>
          )}
          <div className="flex items-start gap-2">
            <span className="mt-1.5 shrink-0 text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
              Lokalita
            </span>
            <div className="min-w-0 flex-1 max-w-xl">
              <LocationTypeahead
                value={districts}
                onChange={(n) => setDistricts(n ?? [])}
              />
            </div>
          </div>
          {/* Sort joins the existing Stav/Typ/Lokalita chip grammar as a fourth
              row rather than floating in a new header toolbar — it is another
              knob on the same cohort, and the operator's eye is already here.
              Ordering applies WITHIN each column. */}
          <div className="flex items-center gap-2">
            <span className="shrink-0 text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
              Řazení
            </span>
            <select
              aria-label="Řazení karet ve fázi"
              value={
                PIPELINE_SORT_OPTIONS.find(
                  (o) => o.field === sort.field && o.direction === sort.direction,
                )?.value ?? sortParamOf(sort)
              }
              onChange={(e) => {
                const picked = PIPELINE_SORT_OPTIONS.find(
                  (o) => o.value === e.target.value,
                );
                if (picked) setSort({ field: picked.field, direction: picked.direction });
              }}
              className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-2 py-1 text-[0.78rem] text-[var(--color-ink-2)] transition-colors hover:border-[var(--color-rule-strong)] focus:border-[var(--color-rule-strong)] focus:outline-none"
            >
              {PIPELINE_SORT_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
        </div>
      )}

      {stagesQ.error || boardQ.error ? (
        <p className="mt-8 text-sm text-[var(--color-brick)]">
          Nepodařilo se načíst pipeline.
        </p>
      ) : boardQ.isLoading ? (
        /* The stages arrive in their own (cached, often already-warm) query, so
           the columns can be drawn — labelled, coloured, in order — while the
           cards are still in flight. That is the whole shape of this page, and
           it lands ~0.35s before the cards do; a bare "Načítání…" threw that
           away and made an interactive board look like a blank screen. */
        <BoardSkeleton stages={stages} />
      ) : cards.length === 0 ? (
        <p className="mt-8 text-sm text-[var(--color-ink-3)]">
          Zatím prázdné. Přidejte nemovitost do pipeline tlačítkem „Přidat do
          pipeline" na detailu inzerátu.
        </p>
      ) : (
        <CardHydrationProvider listingIds={visibleListingIds}>
          <Board
            stages={stages}
            cards={filteredCards}
            byStage={byStage}
            cityQuality={cityQuality}
          />
        </CardHydrationProvider>
      )}
    </div>
  );
}

/* The board's shape, drawn from the stage list alone.
 *
 * Not a generic shimmer: it is the real column layout with the real labels and
 * colours, so the transition to the loaded board is the cards appearing inside
 * columns that were already there — no reflow, no jump. Falls back to three
 * neutral columns on the rare path where even the stages are cold, which keeps
 * the page from collapsing to a single line of text. */
function BoardSkeleton({ stages }: { stages: PipelineStage[] }) {
  const columns: Array<PipelineStage | null> =
    stages.length > 0 ? stages : [null, null, null];
  return (
    <div className="mt-6 flex gap-4 overflow-x-auto pb-4" aria-busy="true">
      {columns.map((s, i) => (
        <div key={s?.id ?? `skeleton-${i}`} className="w-72 shrink-0">
          <div
            className="flex items-baseline justify-between px-1 pb-2 border-b-2"
            style={{ borderColor: s ? stageColor(s) : 'var(--color-rule)' }}
          >
            <span
              className="text-[0.72rem] tracking-[0.14em] uppercase font-medium"
              style={{ color: s ? stageColor(s) : 'var(--color-ink-4)' }}
            >
              {s?.label ?? ' '}
            </span>
          </div>
          <ul className="mt-3 min-h-24 space-y-2 p-1">
            {[0, 1].map((n) => (
              <li
                key={n}
                className="h-[4.5rem] rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] opacity-60"
              />
            ))}
          </ul>
        </div>
      ))}
      <span className="sr-only">Načítání pipeline…</span>
    </div>
  );
}

function stageColor(stage: PipelineStage): string {
  return stage.color
    ? `var(--color-tag-${stage.color})`
    : 'var(--color-rule-strong)';
}

/* Pure: resolve a drag-end (active card, over column) into a stage move, or
 * null for a no-op (same column / dropped outside a column / unknown card).
 * Exported so the move-resolution logic is unit-tested without simulating DnD. */
export function planMove(
  activeId: string,
  overId: string | null,
  cards: PipelineBoardCard[],
): { propertyId: number; stageId: number } | null {
  if (!overId || !overId.startsWith(STAGE_PREFIX)) return null;
  const propertyId = Number(activeId.slice(CARD_PREFIX.length));
  const stageId = Number(overId.slice(STAGE_PREFIX.length));
  if (!Number.isFinite(propertyId) || !Number.isFinite(stageId)) return null;
  const card = cards.find((c) => c.property_id === propertyId);
  if (!card || card.stage_id === stageId) return null;
  return { propertyId, stageId };
}

function Board({
  stages,
  cards,
  byStage,
  cityQuality,
}: {
  stages: PipelineStage[];
  cards: PipelineBoardCard[];
  byStage: Map<number, PipelineBoardCard[]>;
  cityQuality: CityQualityByObec;
}) {
  const qc = useQueryClient();
  const [activeId, setActiveId] = useState<string | null>(null);
  const sensors = useSensors(
    // distance:6 so a click on the card's link/select doesn't start a drag.
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(KeyboardSensor),
  );

  /* Optimistic: the card jumps to the new column instantly (Trello feel),
   * rolled back on error, reconciled on settle — all through the shared cache
   * policy (lib/pipelineCache), so a drag here repaints the Browse funnels and
   * the listing header exactly like a click there repaints the board. The
   * board's own copy patched `board` alone and invalidated `board` alone, which
   * left every Browse funnel badging the pre-drag stage.
   *
   * Rollback rides `onSettled` rather than `onError` on purpose: a mutation
   * that declares `onError` opts out of the app's global error toast
   * (main.tsx), so a failed drag used to snap back with no explanation. */
  const move = useMutation({
    mutationFn: ({ propertyId, stageId }: { propertyId: number; stageId: number }) =>
      movePipelineCard(propertyId, stageId),
    onMutate: ({ propertyId, stageId }) => {
      const stage = cachedStage(qc, stageId);
      return stage ? placeCard(qc, propertyId, stage) : NO_ROLLBACK;
    },
    onSettled: (_d, err, { propertyId }, rollback) => {
      if (err) rollback?.();
      revalidatePipeline(qc, propertyId);
    },
  });

  // Remove a property from the pipeline entirely (the trash action on a card).
  const remove = useMutation({
    mutationFn: (propertyId: number) => removePipelineCard(propertyId),
    onMutate: (propertyId) => dropCard(qc, propertyId),
    onSettled: (_d, err, propertyId, rollback) => {
      if (err) rollback?.();
      revalidatePipeline(qc, propertyId);
    },
  });

  const activeCard = activeId
    ? cards.find((c) => `${CARD_PREFIX}${c.property_id}` === activeId) ?? null
    : null;

  return (
    <DndContext
      sensors={sensors}
      onDragStart={(e: DragStartEvent) => setActiveId(String(e.active.id))}
      onDragCancel={() => setActiveId(null)}
      onDragEnd={(e: DragEndEvent) => {
        setActiveId(null);
        const plan = planMove(
          String(e.active.id),
          e.over ? String(e.over.id) : null,
          cards,
        );
        if (plan) move.mutate(plan);
      }}
    >
      <div className="mt-6 flex gap-4 overflow-x-auto pb-4">
        {stages.map((s) => (
          <StageColumn
            key={s.id}
            stage={s}
            cards={byStage.get(s.id) ?? []}
            cityQuality={cityQuality}
            onRemove={(propertyId) => remove.mutate(propertyId)}
          />
        ))}
      </div>
      {/* dropAnimation={null}: the optimistic move already places the card in
          the target column on release, so the default "fly back to origin"
          drop animation would show the ghost sliding home before the card
          reappears — a visible jump back. Vanish the overlay instantly. */}
      <DragOverlay dropAnimation={null}>
        {activeCard ? (
          <div className="w-64 rounded-[var(--radius-md)] border border-[var(--color-rule-strong)] bg-[var(--color-paper-2)] p-2.5 shadow-lg">
            <CardFace card={activeCard} cityQuality={cityQuality} />
          </div>
        ) : null}
      </DragOverlay>
    </DndContext>
  );
}

function StageManager({ stages }: { stages: PipelineStage[] }) {
  const qc = useQueryClient();
  const [err, setErr] = useState<string | null>(null);
  const [newLabel, setNewLabel] = useState('');

  /* Narrowed from a wholesale `['pipeline']` sweep to the two caches a stage
   * edit can actually change: the stage list itself, and the board (whose
   * columns and badges render from it). The old prefix also swept
   * `card(id)` and `members` — and, had the decoration keys been nested under
   * it, every thumbnail and broker line on the board as well. They are not
   * (lib/hydration owns its own namespace, pinned by hydration.test.ts), but
   * the sweep was still wider than the fact that changed. */
  const invalidate = () => {
    setErr(null);
    void qc.invalidateQueries({ queryKey: pipelineKeys.stages });
    void qc.invalidateQueries({ queryKey: pipelineKeys.board });
  };
  const onError = (e: unknown) =>
    setErr(e instanceof Error ? e.message : 'Akce selhala.');

  const reorder = useMutation({
    mutationFn: (ids: number[]) => reorderPipelineStages(ids),
    onSuccess: invalidate,
    onError,
  });
  const create = useMutation({
    mutationFn: (label: string) => createPipelineStage({ label }),
    onSuccess: () => {
      setNewLabel('');
      invalidate();
    },
    onError,
  });

  const move = (idx: number, dir: -1 | 1) => {
    const ids = stages.map((s) => s.id);
    const j = idx + dir;
    if (j < 0 || j >= ids.length) return;
    [ids[idx], ids[j]] = [ids[j], ids[idx]];
    reorder.mutate(ids);
  };

  const submitNew = () => {
    const label = newLabel.trim();
    if (label) create.mutate(label);
  };

  return (
    <section className="mt-5 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] p-4">
      <p className="text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
        Fáze pipeline
      </p>
      {err && (
        <p className="mt-2 text-xs text-[var(--color-brick)]">{err}</p>
      )}
      <ul className="mt-3 space-y-2">
        {stages.map((s, i) => (
          <StageEditorRow
            key={s.id}
            stage={s}
            ordinal={i + 1}
            isFirst={i === 0}
            isLast={i === stages.length - 1}
            onMove={(dir) => move(i, dir)}
            onError={onError}
            invalidate={invalidate}
          />
        ))}
      </ul>
      <div className="mt-4 flex items-center gap-2 border-t border-[var(--color-rule)] pt-3">
        <input
          value={newLabel}
          onChange={(e) => setNewLabel(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') submitNew();
          }}
          placeholder="Nová fáze…"
          maxLength={80}
          className="flex-1 px-2 py-1 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] focus:outline-none focus:border-[var(--color-rule-strong)]"
        />
        <button
          type="button"
          onClick={submitNew}
          disabled={!newLabel.trim() || create.isPending}
          className="text-[0.72rem] tracking-[0.1em] uppercase px-3 py-1.5 rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)] hover:text-[var(--color-ink)] disabled:opacity-50"
        >
          Přidat
        </button>
      </div>
    </section>
  );
}

function StageEditorRow({
  stage,
  ordinal,
  isFirst,
  isLast,
  onMove,
  onError,
  invalidate,
}: {
  stage: PipelineStage;
  /* Ordinal among the live stages — the placeholder shown in the code box when
     the operator hasn't set one, i.e. exactly what the funnels will render. */
  ordinal: number;
  isFirst: boolean;
  isLast: boolean;
  onMove: (dir: -1 | 1) => void;
  onError: (e: unknown) => void;
  invalidate: () => void;
}) {
  const [label, setLabel] = useState(stage.label);
  const [code, setCode] = useState(stage.code ?? '');

  const update = useMutation({
    mutationFn: (patch: {
      label?: string;
      color?: TagColor | null;
      is_terminal?: boolean;
      is_entry?: boolean;
      code?: string | null;
    }) => updatePipelineStage(stage.id, patch),
    onSuccess: invalidate,
    onError,
  });
  const archive = useMutation({
    mutationFn: () => archivePipelineStage(stage.id),
    onSuccess: invalidate,
    onError,
  });

  const saveLabel = () => {
    const next = label.trim();
    if (next && next !== stage.label) update.mutate({ label: next });
    else setLabel(stage.label);
  };

  /* Empty box = no code: the badge falls back to the ordinal rather than
   * freezing a guessed number into the row (migration 377). */
  const saveCode = () => {
    const next = code.trim();
    if (next === (stage.code ?? '')) return;
    update.mutate({ code: next === '' ? null : next });
  };

  return (
    <li className="space-y-2 py-1">
      <div className="flex items-center gap-2">
        <span
          className="h-4 w-1 shrink-0 rounded-full"
          style={{ background: stageColor(stage) }}
          aria-hidden
        />
        <div className="flex shrink-0 flex-col leading-none">
          <button
            type="button"
            onClick={() => onMove(-1)}
            disabled={isFirst}
            aria-label="Posunout nahoru"
            className="text-[0.6rem] text-[var(--color-ink-3)] hover:text-[var(--color-ink)] disabled:opacity-25"
          >
            ▲
          </button>
          <button
            type="button"
            onClick={() => onMove(1)}
            disabled={isLast}
            aria-label="Posunout dolů"
            className="text-[0.6rem] text-[var(--color-ink-3)] hover:text-[var(--color-ink)] disabled:opacity-25"
          >
            ▼
          </button>
        </div>
        <input
          value={code}
          onChange={(e) => setCode(e.target.value)}
          onBlur={saveCode}
          onKeyDown={(e) => {
            if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
          }}
          maxLength={4}
          placeholder={String(ordinal)}
          aria-label="Značka fáze"
          title={'Značka ve trychtýři (např. „1“, „9“). Prázdné = pořadí fáze.'}
          className="w-10 shrink-0 rounded-[var(--radius-sm)] border border-transparent bg-[var(--color-inset)] px-1 py-1 text-center font-mono text-sm tabular-nums text-[var(--color-ink)] hover:border-[var(--color-rule)] focus:border-[var(--color-rule-strong)] focus:outline-none"
        />
        <input
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          onBlur={saveLabel}
          onKeyDown={(e) => {
            if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
          }}
          maxLength={80}
          aria-label="Název fáze"
          className="flex-1 min-w-0 px-2 py-1 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-transparent hover:border-[var(--color-rule)] focus:border-[var(--color-rule-strong)] focus:outline-none text-[var(--color-ink)]"
        />
        <span className="flex shrink-0 items-center gap-0.5">
          <button
            type="button"
            onClick={() => !stage.is_entry && update.mutate({ is_entry: true })}
            disabled={stage.is_entry}
            title={stage.is_entry ? 'Vstupní fáze' : 'Nastavit jako vstupní'}
            aria-label="Vstupní fáze"
            className="flex w-5 justify-center disabled:cursor-default"
            style={{ color: stage.is_entry ? 'var(--color-copper)' : 'var(--color-ink-4)' }}
          >
            <FunnelIcon filled={stage.is_entry} className="h-4 w-4" />
          </button>
          <Hint text={'Vstupní fáze: sem se nemovitost přidá jako záložka („Přidat do pipeline“). Právě jedna fáze může být vstupní.'} />
        </span>
        <span className="flex shrink-0 items-center gap-0.5 text-[0.68rem] text-[var(--color-ink-3)]">
          <label className="flex items-center gap-1">
            <input
              type="checkbox"
              checked={stage.is_terminal}
              onChange={(e) => update.mutate({ is_terminal: e.target.checked })}
              disabled={stage.is_entry}
            />
            konec
          </label>
          <Hint text={'Koncová fáze: uzavřený obchod (např. Koupeno / Zamítnuto). Při slučování duplicit nepřebije živý (otevřený) obchod.'} />
        </span>
        <button
          type="button"
          onClick={() => archive.mutate()}
          disabled={stage.is_entry || archive.isPending}
          title={
            stage.is_entry
              ? 'Vstupní fázi nelze archivovat'
              : 'Archivovat fázi (musí být prázdná)'
          }
          aria-label="Archivovat fázi"
          className="shrink-0 w-6 text-center text-[var(--color-ink-4)] hover:text-[var(--color-brick)] disabled:opacity-25"
        >
          ✕
        </button>
      </div>
      <div className="flex flex-wrap items-center gap-1 pl-[1.4rem]">
        <TagColorPicker
          value={stage.color ?? null}
          onChange={(c) => update.mutate({ color: c })}
          showNull
          size="sm"
        />
      </div>
    </li>
  );
}

/* Small (i) help glyph — native title hover box (the codebase's tooltip
 * convention) + aria-label so it reads to assistive tech. */
function Hint({ text }: { text: string }) {
  return (
    <span
      role="img"
      aria-label={text}
      title={text}
      className="cursor-help text-[var(--color-ink-3)] hover:text-[var(--color-ink)]"
    >
      <InfoIcon className="h-3.5 w-3.5" />
    </span>
  );
}

function StageColumn({
  stage,
  cards,
  cityQuality,
  onRemove,
}: {
  stage: PipelineStage;
  cards: PipelineBoardCard[];
  cityQuality: CityQualityByObec;
  onRemove: (propertyId: number) => void;
}) {
  const { setNodeRef, isOver } = useDroppable({ id: `${STAGE_PREFIX}${stage.id}` });
  return (
    <div className="w-72 shrink-0">
      <div
        className="flex items-baseline justify-between px-1 pb-2 border-b-2"
        style={{ borderColor: stageColor(stage) }}
      >
        <span
          className="text-[0.72rem] tracking-[0.14em] uppercase font-medium"
          style={{ color: stageColor(stage) }}
        >
          {stage.label}
        </span>
        <span className="font-mono tabular-nums text-[0.7rem] text-[var(--color-ink-4)]">
          {cards.length}
        </span>
      </div>
      <ul
        ref={setNodeRef}
        className={`mt-3 min-h-24 space-y-2 rounded-[var(--radius-md)] p-1 transition-colors ${
          isOver
            ? 'bg-[var(--color-inset)] outline outline-1 outline-[var(--color-rule-strong)]'
            : ''
        }`}
      >
        {cards.length === 0 ? (
          <li className="px-1 py-2 text-sm text-[var(--color-ink-4)]">—</li>
        ) : (
          cards.map((c) => (
            <li key={c.property_id}>
              <BoardCard card={c} cityQuality={cityQuality} onRemove={onRemove} />
            </li>
          ))
        )}
      </ul>
    </div>
  );
}
