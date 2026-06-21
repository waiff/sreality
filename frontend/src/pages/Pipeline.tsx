import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  DndContext,
  DragOverlay,
  KeyboardSensor,
  PointerSensor,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
  type DragEndEvent,
  type DragStartEvent,
} from '@dnd-kit/core';
import { CSS } from '@dnd-kit/utilities';
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
import { LocationTypeahead } from '@/components/filter-controls/LocationTypeahead';
import { type DistrictChip } from '@/lib/filters';
import { fmtArea, fmtCzk } from '@/lib/format';
import { listingPath } from '@/lib/listingUrl';
import { FILTER_REGISTRY } from '@/lib/filterRegistry.generated';
import TagColorPicker from '@/components/TagColorPicker';
import { FunnelIcon, InfoIcon, TrashIcon } from '@/components/icons';
import {
  type PipelineBoardCard,
  type PipelineCardBroker,
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

export default function Pipeline() {
  const [manage, setManage] = useState(false);
  const [types, setTypes] = useState<Set<string>>(new Set());
  const [districts, setDistricts] = useState<DistrictChip[]>([]);
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

  // Client-side filters (the board is small, rule #22): type chips + the region
  // picker, applied in-memory. Region reuses Browse's exact chip semantics via
  // matchesDistricts. Empty = no constraint.
  const filtersActive = types.size > 0 || districts.length > 0;
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
    return result;
  }, [boardQ.data, types, districts]);

  const byStage = useMemo(() => {
    const m = new Map<number, PipelineBoardCard[]>();
    for (const s of stagesQ.data ?? []) m.set(s.id, []);
    for (const c of filteredCards) {
      const bucket = m.get(c.stage_id);
      if (bucket) bucket.push(c);
    }
    return m;
  }, [stagesQ.data, filteredCards]);

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
            {filtersActive
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

      {/* Filters — property type (only when the pipeline holds >1 type) + the
          region picker. The region control is the SAME LocationTypeahead Browse
          and Datasets use; both filters apply client-side (rule #22, the board
          is small). */}
      {cards.length > 0 && (
        <div className="mt-5 flex flex-col gap-3">
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
                    onClick={() =>
                      setTypes((prev) => {
                        const next = new Set(prev);
                        if (next.has(t)) next.delete(t);
                        else next.add(t);
                        return next;
                      })
                    }
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
                  onClick={() => setTypes(new Set())}
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
        </div>
      )}

      {stagesQ.isLoading || boardQ.isLoading ? (
        <p className="mt-8 text-sm text-[var(--color-ink-3)]">Načítání…</p>
      ) : stagesQ.error || boardQ.error ? (
        <p className="mt-8 text-sm text-[var(--color-brick)]">
          Nepodařilo se načíst pipeline.
        </p>
      ) : cards.length === 0 ? (
        <p className="mt-8 text-sm text-[var(--color-ink-3)]">
          Zatím prázdné. Přidejte nemovitost do pipeline tlačítkem „Přidat do
          pipeline" na detailu inzerátu.
        </p>
      ) : (
        <Board stages={stages} cards={filteredCards} byStage={byStage} />
      )}
    </div>
  );
}

function stageColor(stage: PipelineStage): string {
  return stage.color
    ? `var(--color-tag-${stage.color})`
    : 'var(--color-rule-strong)';
}

const CARD_PREFIX = 'card:';
const STAGE_PREFIX = 'stage:';

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
}: {
  stages: PipelineStage[];
  cards: PipelineBoardCard[];
  byStage: Map<number, PipelineBoardCard[]>;
}) {
  const qc = useQueryClient();
  const [activeId, setActiveId] = useState<string | null>(null);
  const sensors = useSensors(
    // distance:6 so a click on the card's link/select doesn't start a drag.
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(KeyboardSensor),
  );

  const move = useMutation({
    mutationFn: ({ propertyId, stageId }: { propertyId: number; stageId: number }) =>
      movePipelineCard(propertyId, stageId),
    // Optimistic: the card jumps to the new column instantly (Trello feel),
    // rolled back on error, reconciled on settle.
    onMutate: async ({ propertyId, stageId }) => {
      await qc.cancelQueries({ queryKey: pipelineKeys.board });
      const prev = qc.getQueryData<PipelineBoardCard[]>(pipelineKeys.board);
      qc.setQueryData<PipelineBoardCard[]>(pipelineKeys.board, (old) =>
        (old ?? []).map((c) =>
          c.property_id === propertyId ? { ...c, stage_id: stageId } : c,
        ),
      );
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(pipelineKeys.board, ctx.prev);
    },
    onSettled: () => qc.invalidateQueries({ queryKey: pipelineKeys.board }),
  });

  // Remove a property from the pipeline entirely (the trash action on a card).
  // Optimistic: drop it from the board immediately; reconcile board + the
  // shared members-set (Browse-card funnels) on settle.
  const remove = useMutation({
    mutationFn: (propertyId: number) => removePipelineCard(propertyId),
    onMutate: async (propertyId) => {
      await qc.cancelQueries({ queryKey: pipelineKeys.board });
      const prev = qc.getQueryData<PipelineBoardCard[]>(pipelineKeys.board);
      qc.setQueryData<PipelineBoardCard[]>(pipelineKeys.board, (old) =>
        (old ?? []).filter((c) => c.property_id !== propertyId),
      );
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(pipelineKeys.board, ctx.prev);
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: pipelineKeys.board });
      qc.invalidateQueries({ queryKey: pipelineKeys.members });
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
            <CardFace card={activeCard} />
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

  const invalidate = () => {
    setErr(null);
    void qc.invalidateQueries({ queryKey: ['pipeline'] });
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
  isFirst,
  isLast,
  onMove,
  onError,
  invalidate,
}: {
  stage: PipelineStage;
  isFirst: boolean;
  isLast: boolean;
  onMove: (dir: -1 | 1) => void;
  onError: (e: unknown) => void;
  invalidate: () => void;
}) {
  const [label, setLabel] = useState(stage.label);

  const update = useMutation({
    mutationFn: (patch: {
      label?: string;
      color?: TagColor | null;
      is_terminal?: boolean;
      is_entry?: boolean;
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
  onRemove,
}: {
  stage: PipelineStage;
  cards: PipelineBoardCard[];
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
              <BoardCard card={c} onRemove={onRemove} />
            </li>
          ))
        )}
      </ul>
    </div>
  );
}

function CardThumb({ url }: { url: string | null }) {
  const cls =
    'h-12 w-12 shrink-0 rounded-[var(--radius-sm)] border border-[var(--color-rule)]';
  if (!url) return <div className={`${cls} bg-[var(--color-inset)]`} aria-hidden />;
  return <img src={url} alt="" loading="lazy" className={`${cls} object-cover`} />;
}

/* The card's visible content — reused by the in-column card and the drag ghost.
 * Thumbnail + price + street/district + disposition·area + MF gross yield. The
 * image + yield reuse the same resolution/format Browse cards use (broker is a
 * deferred follow-up — needs a batched canonical-broker lookup). */
function CardFace({ card }: { card: PipelineBoardCard }) {
  const place = [card.street, card.district].filter(Boolean).join(', ');
  const dims = [
    card.disposition,
    card.area_m2 != null ? fmtArea(card.area_m2) : null,
  ]
    .filter(Boolean)
    .join(' · ');
  return (
    <div className="flex gap-2.5">
      <CardThumb url={card.image_url} />
      <div className="min-w-0 flex-1">
        {card.sreality_id != null ? (
          <Link
            to={listingPath(card.sreality_id)}
            className="font-mono tabular-nums text-sm text-[var(--color-ink)] hover:text-[var(--color-copper)] hover:underline underline-offset-2"
          >
            {fmtCzk(card.price_czk)}
          </Link>
        ) : (
          <span className="font-mono tabular-nums text-sm text-[var(--color-ink)]">
            {fmtCzk(card.price_czk)}
          </span>
        )}
        {place && (
          <p className="mt-0.5 truncate text-xs text-[var(--color-ink-2)]">{place}</p>
        )}
        <div className="mt-0.5 flex items-center justify-between gap-2">
          <span className="truncate font-mono tabular-nums text-xs text-[var(--color-ink-4)]">
            {dims || '—'}
          </span>
          {card.mf_gross_yield_pct != null && (
            <span
              className="shrink-0 font-mono tabular-nums text-[0.68rem] text-[var(--color-ink-3)]"
              title="Hrubý výnos dle cenové mapy nájemného MF"
            >
              MF{' '}
              {card.mf_gross_yield_pct.toLocaleString('cs-CZ', {
                minimumFractionDigits: 1,
                maximumFractionDigits: 1,
              })}{' '}
              %
            </span>
          )}
        </div>
        {card.broker && (
          <p className="mt-0.5 truncate text-[0.7rem] text-[var(--color-ink-3)]">
            <Link
              to={`/brokers/${card.broker.broker_id}`}
              title={brokerHoverTitle(card.broker)}
              className="hover:text-[var(--color-copper)] hover:underline underline-offset-2"
            >
              {card.broker.display_name ?? 'Makléř'}
            </Link>
            {card.broker.firm_label && (
              <span className="text-[var(--color-ink-4)]"> · {card.broker.firm_label}</span>
            )}
          </p>
        )}
      </div>
    </div>
  );
}

/* Native-title hover box for a card's broker — name, firm, and contact on one
 * line (the codebase's tooltip convention). The name itself links to the broker
 * page for the full record. */
function brokerHoverTitle(b: PipelineCardBroker): string {
  return (
    [b.display_name, b.firm_label, b.phone, b.email].filter(Boolean).join(' · ') ||
    'Zobrazit makléře'
  );
}

function BoardCard({
  card,
  onRemove,
}: {
  card: PipelineBoardCard;
  onRemove: (propertyId: number) => void;
}) {
  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({
    id: `${CARD_PREFIX}${card.property_id}`,
  });
  const [confirming, setConfirming] = useState(false);
  const style = {
    transform: CSS.Translate.toString(transform),
    opacity: isDragging ? 0.4 : undefined,
  };

  return (
    <div
      ref={setNodeRef}
      style={style}
      className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] p-2.5"
    >
      <div className="flex items-start gap-1.5">
        <button
          type="button"
          {...attributes}
          {...listeners}
          aria-label="Přetáhnout kartu do jiné fáze"
          className="shrink-0 cursor-grab touch-none pt-0.5 leading-none text-[var(--color-ink-4)] hover:text-[var(--color-ink-2)] active:cursor-grabbing"
        >
          ⠿
        </button>
        <div className="min-w-0 flex-1">
          <CardFace card={card} />
        </div>
        <button
          type="button"
          onClick={() => setConfirming((v) => !v)}
          aria-label="Odebrat z pipeline"
          aria-expanded={confirming}
          title="Odebrat z pipeline"
          className="shrink-0 rounded-[var(--radius-xs)] pt-0.5 text-[var(--color-ink-4)] hover:text-[var(--color-brick)] focus-visible:outline focus-visible:outline-1 focus-visible:outline-offset-1 focus-visible:outline-[var(--color-rule-strong)]"
        >
          <TrashIcon className="h-3.5 w-3.5" />
        </button>
      </div>
      {/* Inline two-step confirm (the app's destructive-action pattern) — removing
          a property from the pipeline drops the card entirely. Stage moves are
          drag-only now; the select fallback was removed. */}
      {confirming && (
        <div className="mt-2 flex items-center gap-2 border-t border-[var(--color-rule-soft)] pt-2 text-[0.72rem]">
          <span className="mr-auto text-[var(--color-ink-3)]">Odebrat z pipeline?</span>
          <button
            type="button"
            onClick={() => {
              setConfirming(false);
              onRemove(card.property_id);
            }}
            className="rounded-[var(--radius-sm)] border border-[var(--color-brick)] px-2 py-0.5 text-[var(--color-brick)] hover:bg-[var(--color-brick)]/10"
          >
            Odebrat
          </button>
          <button
            type="button"
            onClick={() => setConfirming(false)}
            className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-2 py-0.5 text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)] hover:bg-[var(--color-rule-soft)]"
          >
            Zrušit
          </button>
        </div>
      )}
    </div>
  );
}
