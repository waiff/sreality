import { useMemo, useRef, useState, type ReactNode } from 'react';
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
  listCollections,
  movePipelineCard,
  removePipelineCard,
  reorderPipelineStages,
  updatePipelineStage,
} from '@/lib/api';
import {
  curationKeys,
  fetchPipelineBoard,
  fetchPipelineStages,
  fetchPropertyCollectionMemberSet,
  matchesDistricts,
  pipelineKeys,
} from '@/lib/queries';
import { NO_ROLLBACK } from '@/lib/optimisticCache';
import {
  cachedStage,
  dropCard,
  placeCard,
  revalidatePipeline,
} from '@/lib/pipelineCache';
import { CardHydrationProvider } from '@/lib/hydration';
import { useLegacyChipUpgrade } from '@/lib/useLegacyChipUpgrade';
import { LocationTypeahead } from '@/components/filter-controls/LocationTypeahead';
import { MultiselectChips } from '@/components/filter-controls/MultiselectChips';
import { type EnumOptionLite } from '@/components/filter-controls/types';
import { Field, Segmented } from '@/components/controls';
import { matchesCollections } from '@/lib/collectionScope';
import { type ListingStatus } from '@/lib/filters';
import { filterById } from '@/lib/filterRegistry.generated';
import TagColorPicker from '@/components/TagColorPicker';
import { FunnelIcon, InfoIcon } from '@/components/icons';
import SizeToggle, {
  LargeImageGlyph,
  MediumImageGlyph,
  SmallImageGlyph,
} from '@/components/SizeToggle';
import {
  PIPELINE_CARD_GEOMETRY,
  PIPELINE_CARD_SIZE_LABELS,
  PIPELINE_CARD_SIZES,
  usePipelineCardSize,
  type PipelineCardSize,
} from '@/lib/pipelineCardSize';
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

/* The bar's vocabulary comes off the generated filter registry, so the
 * Byty/Domy/… and Vše/Aktivní/Neaktivní labels never drift from Browse's. */
const enumOptions = (id: string): EnumOptionLite[] =>
  (filterById(id)?.enum_values ?? []).map((o) => ({
    value: String(o.value),
    label: o.label_cs,
  }));

/* `status` reads against the property-grain `is_active` rollup (bool_or over
 * child listings, rule #15/#20) that fetchPipelineBoard already selects. */
const STATUS_OPTIONS = enumOptions('status').map((o) => ({
  value: o.value as ListingStatus,
  label: o.label,
}));
const CATEGORY_MAIN_OPTIONS = enumOptions('category_main');

/* One stable empty board, so every memo below can depend on `cards` itself. */
const NO_CARDS: PipelineBoardCard[] = [];

export default function Pipeline() {
  const [manage, setManage] = useState(false);
  /* Filters AND sort live in the URL (lib/pipelineState) using Browse's own
   * param vocabulary, so a filtered/sorted board is linkable and survives a
   * reload. `manage` stays local — it's a transient editor toggle, not a view. */
  const {
    status,
    types,
    districts,
    collectionIds,
    sort,
    setStatus,
    setTypes,
    setDistricts,
    setCollections,
    setSort,
    reset,
  } = usePipelineViewState();
  /* The board filters its cards in the browser (matchesDistricts), so a chip
   * with no code matches nothing here exactly as it does server-side — which
   * means a URL carrying a pre-code chip has to be upgraded on THIS surface
   * too. Without it the same link shows a cohort on /browse and an empty board
   * here, and the kanban looks broken rather than unfiltered. */
  useLegacyChipUpgrade(districts, setDistricts);
  /* Card size is a workspace preference, NOT part of the URL view state above:
   * a shared link carries which deals to look at, not how this browser likes
   * its photos. Same split Browse draws for its own image-size switch. */
  const cardSize = usePipelineCardSize();
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

  /* The collection lens (rule #18), on the two keys the rest of the app
   * already shares — the board pays for them only on a cold visit. */
  const collectionsQ = useQuery({
    queryKey: curationKeys.collections,
    queryFn: listCollections,
    staleTime: 30_000,
  });
  const membersQ = useQuery({
    queryKey: curationKeys.propertyCollectionMembers,
    queryFn: fetchPropertyCollectionMemberSet,
    staleTime: 30_000,
  });
  /* Undefined while loading or errored — NOT an empty map (the fail-open
   * contract, docs/architecture.md rule #22). */
  const members = membersQ.data;

  const stages = stagesQ.data ?? [];
  const cards = boardQ.data ?? NO_CARDS;

  // Client-side (rule #22) as ONE list the board, the count and Reset share.
  // The collection clause is skipped while the member map is unresolved.
  const predicates = useMemo(() => {
    const out: Array<(c: PipelineBoardCard) => boolean> = [];
    if (types.size > 0) {
      out.push((c) => c.category_main != null && types.has(c.category_main));
    }
    if (districts.length > 0) out.push((c) => matchesDistricts(c, districts));
    if (status !== 'any') out.push((c) => c.is_active === (status === 'active'));
    if (members && collectionIds.length > 0) {
      out.push((c) => matchesCollections(members.get(c.property_id), collectionIds));
    }
    return out;
  }, [types, districts, status, members, collectionIds]);

  const filtersActive = predicates.length > 0;
  const filteredCards = useMemo(
    () => cards.filter((c) => predicates.every((p) => p(c))),
    [cards, predicates],
  );

  /* One pass over the board for everything the bar offers: the types on it,
   * whether a delisted card is on it, and which collections hold one. */
  const bar = useMemo(() => {
    const present = new Set<string>();
    const held = new Set<number>();
    let inactive = 0;
    let inCollection = 0;
    for (const c of cards) {
      if (c.category_main) present.add(c.category_main);
      if (!c.is_active) inactive += 1;
      const ids = members?.get(c.property_id);
      if (ids?.length) {
        inCollection += 1;
        for (const id of ids) held.add(id);
      }
    }
    const known = collectionsQ.data?.data ?? [];
    /* Only collections with a member ON the board — plus any SELECTED one,
     * whose chip has to render or the constraint is un-deselectable. */
    const onBoard = known.filter((c) => held.has(c.id));
    const options = onBoard.map((c) => ({ value: c.id, label: c.name }));
    for (const id of collectionIds) {
      if (options.some((o) => o.value === id)) continue;
      options.push({ value: id, label: known.find((c) => c.id === id)?.name ?? `#${id}` });
    }
    return {
      types: CATEGORY_MAIN_OPTIONS.filter((o) => present.has(o.value)),
      hasInactive: inactive > 0,
      collections: options,
      /* A live constraint is always visible; otherwise the row is offered only
       * when it could change the view. Membership is multi-valued and partial,
       * so an option count would wrongly hide a working control — two
       * collections can partition a board with every card inside one. */
      showCollections:
        !!members &&
        (collectionIds.length > 0 ||
          (onBoard.length > 0 && (inCollection < cards.length || onBoard.length >= 2))),
    };
  }, [cards, members, collectionsQ.data, collectionIds]);

  /* The decoration cohort: the representative listing of every card ON the
   * board (not just the filtered view — filtering is client-side and instant,
   * so hydrating the full board once keeps a filter toggle free instead of
   * re-keying the enrichment query on every chip click). */
  const visibleListingIds = useMemo(
    () => cards.map((c) => c.listing_id).filter((id): id is number => id != null),
    [cards],
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
          {filtersActive && (
            <button
              type="button"
              onClick={reset}
              className="text-[0.7rem] tracking-wide uppercase text-[var(--color-ink-3)] hover:text-[var(--color-copper)] transition-colors"
            >
              Reset
            </button>
          )}
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

      {cards.length > 0 && (
        <div className="mt-5 flex flex-col gap-3">
          <div className="flex flex-wrap items-end gap-x-6 gap-y-3">
            {bar.hasInactive && (
              <Field label="Stav">
                <Segmented
                  variant="solid"
                  options={STATUS_OPTIONS}
                  value={status}
                  onChange={setStatus}
                />
              </Field>
            )}
            {bar.types.length >= 2 && (
              <Field label="Typ">
                <MultiselectChips
                  value={[...types]}
                  options={bar.types}
                  onChange={setTypes}
                />
              </Field>
            )}
            {bar.showCollections && (
              <Field label="Kolekce">
                <MultiselectChips
                  value={collectionIds}
                  options={bar.collections}
                  onChange={setCollections}
                />
              </Field>
            )}
            <Field label="Lokalita" className="min-w-[16rem] max-w-xl flex-1">
              <LocationTypeahead
                label="Lokalita"
                value={districts}
                onChange={(n) => setDistricts(n ?? [])}
              />
            </Field>
          </div>
          {/* Řazení and Karty are how the board PRESENTS the cohort, not which
              deals are in it — their own row, and Reset leaves them alone. */}
          <div className="flex flex-wrap items-end gap-x-6 gap-y-3">
            <Field label="Řazení" as="control">
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
                className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-2 py-1 text-[0.78rem] text-[var(--color-ink-2)] transition-colors hover:border-[var(--color-rule-strong)]"
              >
                {PIPELINE_SORT_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Karty" className="ml-auto">
              <SizeToggle
                label="Velikost karet"
                value={cardSize.value}
                onChange={cardSize.set}
                steps={PIPELINE_CARD_SIZES.map((v) => ({
                  value: v,
                  label: PIPELINE_CARD_SIZE_LABELS[v].label,
                  title: PIPELINE_CARD_SIZE_LABELS[v].title,
                  glyph: CARD_SIZE_GLYPH[v],
                }))}
              />
            </Field>
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
        <BoardSkeleton stages={stages} size={cardSize.value} />
      ) : cards.length === 0 ? (
        <p className="mt-8 text-sm text-[var(--color-ink-3)]">
          Zatím prázdné. Přidejte nemovitost do pipeline tlačítkem „Přidat do
          pipeline" na detailu inzerátu.
        </p>
      ) : (
        <CardHydrationProvider
          listingIds={visibleListingIds}
          /* The board's card face: one 48px thumbnail and a broker line. No
             carousel — asking the multi-image read for one photo per card is
             exactly what W4's listing_cover_public replaced. */
          renders={{ covers: true, brokers: true }}
        >
          <Board
            stages={stages}
            cards={filteredCards}
            byStage={byStage}
            cityQuality={cityQuality}
            size={cardSize.value}
          />
        </CardHydrationProvider>
      )}
    </div>
  );
}

/* One glyph per card size — the switch's vocabulary, shared with the boolean
 * ImageSizeToggle the other grids use. */
const CARD_SIZE_GLYPH: Record<PipelineCardSize, JSX.Element> = {
  sm: <SmallImageGlyph />,
  md: <MediumImageGlyph />,
  lg: <LargeImageGlyph />,
};

/* The board's shape, drawn from the stage list alone.
 *
 * Not a generic shimmer: it is the real column layout with the real labels and
 * colours, so the transition to the loaded board is the cards appearing inside
 * columns that were already there — no reflow, no jump. Falls back to three
 * neutral columns on the rare path where even the stages are cold, which keeps
 * the page from collapsing to a single line of text. */
function BoardSkeleton({
  stages,
  size,
}: {
  stages: PipelineStage[];
  size: PipelineCardSize;
}) {
  const columns: Array<PipelineStage | null> =
    stages.length > 0 ? stages : [null, null, null];
  const geo = PIPELINE_CARD_GEOMETRY[size];
  return (
    <BoardFrame
      busy
      header={columns.map((s, i) => (
        <StageHeader key={s?.id ?? `skeleton-${i}`} stage={s} size={size} />
      ))}
    >
      {columns.map((s, i) => (
        <ul
          key={s?.id ?? `skeleton-${i}`}
          className={`shrink-0 space-y-2 p-1 ${geo.column} ${geo.dropZoneMin}`}
        >
          {[0, 1].map((n) => (
            <li
              key={n}
              className={`${geo.skeletonRow} rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] opacity-60`}
            />
          ))}
        </ul>
      ))}
      <span className="sr-only">Načítání pipeline…</span>
    </BoardFrame>
  );
}

/* The board's two rows: the stage headers, pinned under the top bar while the
 * page scrolls, above the columns, which scroll sideways.
 *
 * The headers cannot simply be `sticky` inside their columns. `overflow-x:
 * auto` forces `overflow-y` to auto as well, so the column row is its own
 * scroll container, and a sticky header inside it pins against that row —
 * which never scrolls vertically — instead of against the page. So the headers
 * live in a row OUTSIDE the scroller and follow it by copying its scrollLeft.
 * Both rows lay out the same column widths and gaps, so their scroll ranges
 * are identical and any clamp (a smaller card size, a narrower window) lands
 * on both alike: a scroll of the columns is the one thing to mirror.
 *
 * `top-14` is the Shell's top-bar height (Shell.tsx lists every site pinned to
 * it). `z-10` keeps the row above the cards but below the Lokalita dropdown
 * (z-20), which can hang down over the board. */
function BoardFrame({
  header,
  busy,
  children,
}: {
  header: ReactNode;
  busy?: boolean;
  children: ReactNode;
}) {
  const headerRow = useRef<HTMLDivElement>(null);
  return (
    <div className="mt-3" aria-busy={busy || undefined}>
      <div
        ref={headerRow}
        className="sticky top-14 z-10 flex gap-4 overflow-hidden bg-[var(--color-paper)] pt-3"
      >
        {header}
      </div>
      {/* items-stretch: every column is as tall as the tallest, so each stage's
          drop zone spans the whole board height instead of ending at its last
          card. */}
      <div
        onScroll={(e) => {
          if (headerRow.current) headerRow.current.scrollLeft = e.currentTarget.scrollLeft;
        }}
        className="mt-3 flex items-stretch gap-4 overflow-x-auto pb-4"
      >
        {children}
      </div>
    </div>
  );
}

/* A stage's label and rule in its colour, plus its card count once the board
 * has loaded. The skeleton's stand-in columns (stages still cold) pass no
 * stage and draw a neutral rule; the no-break space holds the label's line
 * height so the row doesn't collapse. */
function StageHeader({
  stage,
  count,
  size,
}: {
  stage: PipelineStage | null;
  count?: number;
  size: PipelineCardSize;
}) {
  return (
    <div
      className={`flex shrink-0 items-baseline justify-between px-1 pb-2 border-b-2 ${PIPELINE_CARD_GEOMETRY[size].column}`}
      style={{ borderColor: stage ? stageColor(stage) : 'var(--color-rule)' }}
    >
      <span
        className="text-[0.72rem] tracking-[0.14em] uppercase font-medium"
        style={{ color: stage ? stageColor(stage) : 'var(--color-ink-4)' }}
      >
        {stage?.label ?? ' '}
      </span>
      {count != null && (
        <span className="font-mono tabular-nums text-[0.7rem] text-[var(--color-ink-4)]">
          {count}
        </span>
      )}
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
  size,
}: {
  stages: PipelineStage[];
  cards: PipelineBoardCard[];
  byStage: Map<number, PipelineBoardCard[]>;
  cityQuality: CityQualityByObec;
  size: PipelineCardSize;
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
    onSettled: (_d, err, _vars, rollback) => {
      if (err) rollback?.();
      revalidatePipeline(qc);
    },
  });

  // Remove a property from the pipeline entirely (the trash action on a card).
  const remove = useMutation({
    mutationFn: (propertyId: number) => removePipelineCard(propertyId),
    onMutate: (propertyId) => dropCard(qc, propertyId),
    onSettled: (_d, err, _propertyId, rollback) => {
      if (err) rollback?.();
      revalidatePipeline(qc);
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
      <BoardFrame
        header={stages.map((s) => (
          <StageHeader
            key={s.id}
            stage={s}
            count={byStage.get(s.id)?.length ?? 0}
            size={size}
          />
        ))}
      >
        {stages.map((s) => (
          <StageColumn
            key={s.id}
            stage={s}
            cards={byStage.get(s.id) ?? []}
            cityQuality={cityQuality}
            size={size}
            onRemove={(propertyId) => remove.mutate(propertyId)}
          />
        ))}
      </BoardFrame>
      {/* dropAnimation={null}: the optimistic move already places the card in
          the target column on release, so the default "fly back to origin"
          drop animation would show the ghost sliding home before the card
          reappears — a visible jump back. Vanish the overlay instantly. */}
      <DragOverlay dropAnimation={null}>
        {activeCard ? (
          <div
            className={`${PIPELINE_CARD_GEOMETRY[size].overlay} rounded-[var(--radius-md)] border border-[var(--color-rule-strong)] bg-[var(--color-paper-2)] p-2.5 shadow-lg`}
          >
            <CardFace card={activeCard} cityQuality={cityQuality} size={size} />
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
          aria-label="Nová fáze"
          value={newLabel}
          onChange={(e) => setNewLabel(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') submitNew();
          }}
          placeholder="Nová fáze…"
          maxLength={80}
          className="flex-1 px-2 py-1 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)]"
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
          className="w-10 shrink-0 rounded-[var(--radius-sm)] border border-transparent bg-[var(--color-inset)] px-1 py-1 text-center font-mono text-sm tabular-nums text-[var(--color-ink)] hover:border-[var(--color-rule)]"
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
          className="flex-1 min-w-0 px-2 py-1 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-transparent hover:border-[var(--color-rule)] text-[var(--color-ink)]"
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
  size,
  onRemove,
}: {
  stage: PipelineStage;
  cards: PipelineBoardCard[];
  cityQuality: CityQualityByObec;
  size: PipelineCardSize;
  onRemove: (propertyId: number) => void;
}) {
  const { setNodeRef, isOver } = useDroppable({ id: `${STAGE_PREFIX}${stage.id}` });
  const geo = PIPELINE_CARD_GEOMETRY[size];
  /* The stage's header lives in BoardFrame's pinned row, not above this list,
     so the list names its stage itself — otherwise a screen reader meets every
     header first and then a run of unlabelled lists. */
  return (
    <ul
      ref={setNodeRef}
      aria-label={stage.label}
      className={`shrink-0 space-y-2 rounded-[var(--radius-md)] p-1 transition-colors ${geo.column} ${geo.dropZoneMin} ${
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
            <BoardCard
              card={c}
              cityQuality={cityQuality}
              size={size}
              onRemove={onRemove}
            />
          </li>
        ))
      )}
    </ul>
  );
}
