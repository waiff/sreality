/* Sort options for the deal-pipeline board.
 *
 * MANUAL ORDER IS A REAL SORT, AND IT IS THE DEFAULT.
 * `property_pipeline.board_position` is a persisted per-card rank: the API has
 * always accepted it (`PATCH /pipeline/cards/{id}` takes `board_position`), the
 * board has always ordered by it, and the frontend has never once written it.
 * That leaves it dead-but-load-bearing, and worse, meaningless: it is assigned
 * `max+1` within the ENTRY stage at bookmark time and is never renumbered when
 * a card is dragged to another column, so live data has duplicate positions
 * *within* a stage. Ordering by it alone is non-deterministic between refetches.
 *
 * Rather than replace it, this models it as the default option ("Ruční") and
 * gives every comparator the same deterministic tiebreak, so:
 *   - today's behaviour is preserved as an explicit, named choice;
 *   - the ordering stops shuffling;
 *   - if drag-to-reorder-within-a-column is ever wired up, the rule for which
 *     order wins is already stated instead of being invented later.
 * Two orderings cannot both own the vertical axis: an explicit sort overrides
 * the manual one, and a within-column reorder gesture only makes sense while
 * "Ruční" is active.
 *
 * SORTING IS CLIENT-SIDE and that is deliberate — the board fetches every card
 * in one read (41 live; PostgREST's ceiling on this project is 50,000), so
 * there is nothing to paginate and a server round-trip per sort would be pure
 * latency. Browse is the opposite case and keeps its keyset path; the two share
 * the serialization format and the null/collation rules via lib/cardSort, not a
 * comparator.
 */

import {
  byNumber,
  makeSorter,
  timeKey,
  type Accessor,
  type Sort,
  type SortOption,
} from './cardSort';
import { placePrimary } from './placeLabel';
import type { PipelineBoardCard } from './types';

export type PipelineSortField =
  | 'board_position'
  | 'added_at'
  | 'entered_stage_at'
  | 'price_czk'
  | 'total_price_change_pct'
  | 'city';

export type PipelineSort = Sort<PipelineSortField>;

/* Labels are Czech to match the page. Each key offers both directions as
 * separate options (Browse's SORT_PRESETS precedent) rather than a separate
 * direction toggle — one control, no hidden state.
 *
 * "Ve fázi nejdéle" is `entered_stage_at` ASCENDING: the actionable framing of
 * "date stage moved" is which deals have been sitting still longest, not a
 * literal timestamp ordering. */
export const PIPELINE_SORT_OPTIONS: ReadonlyArray<SortOption<PipelineSortField>> = [
  { value: 'manual',            label: 'Ruční pořadí',        field: 'board_position',   direction: 'asc'  },
  { value: '-added_at',         label: 'Přidáno nejnověji',   field: 'added_at',         direction: 'desc' },
  { value: 'added_at',          label: 'Přidáno nejdříve',    field: 'added_at',         direction: 'asc'  },
  { value: 'entered_stage_at',  label: 'Ve fázi nejdéle',     field: 'entered_stage_at', direction: 'asc'  },
  { value: '-entered_stage_at', label: 'Ve fázi nejkratčeji', field: 'entered_stage_at', direction: 'desc' },
  { value: '-price_czk',        label: 'Cena nejvyšší',       field: 'price_czk',        direction: 'desc' },
  { value: 'price_czk',         label: 'Cena nejnižší',       field: 'price_czk',        direction: 'asc'  },
  { value: 'total_price_change_pct',  label: 'Cena nejvíc klesla',   field: 'total_price_change_pct', direction: 'asc'  },
  { value: '-total_price_change_pct', label: 'Cena nejvíc vzrostla', field: 'total_price_change_pct', direction: 'desc' },
  { value: 'city',              label: 'Město A–Ž',           field: 'city',             direction: 'asc'  },
];

export const DEFAULT_PIPELINE_SORT: PipelineSort = {
  field: 'board_position',
  direction: 'asc',
};

/* The "city" key sorts on what the card actually SHOWS. placePrimary() is the
 * app's shared place resolver — it prefers a rich free-text locality, falls back
 * to the geo-derived municipality when that locality is merely the okres name
 * (the Bazoš "Jihlava"-for-Telč case), and only then to district. Sorting on a
 * field the operator cannot see is unverifiable, which is why this is the same
 * call the card's place line makes rather than a bare `obec`. */
const cityKey = (c: PipelineBoardCard): string | null =>
  placePrimary({
    locality: c.locality,
    district: c.district,
    obec: c.obec,
    okres: c.okres,
    street: null, // street would sort by house number, not by town
  });

/* Price movement sorts on the SIGNED percent, not its magnitude, and ASCENDING
 * is offered first: most-negative-first puts the deepest cut at the top. That
 * follows <PriceDelta>'s polarity — this is a buyer's board, so a seller
 * dropping their ask is the actionable event, not a symmetric "biggest mover".
 * An |abs| option would rank a 20% rise alongside a 20% cut and is deliberately
 * not offered; the two directions answer the two real questions separately.
 *
 * NULL IS NOT ZERO, and it is the majority case. `total_price_change_pct` is
 * NULL whenever the representative listing has fewer than two priced snapshots
 * — roughly 60% of live cards, which have been seen exactly once. cardSort's
 * NULLS-LAST-in-both-directions rule sinks them under the movers in either
 * direction, which is the honest placement: "not observed to move" must never
 * outrank an observed 0%, and it must never head the list of biggest risers
 * either. Those cards land at the bottom ordered only by the property_id
 * tiebreak, and the card renders no arrow at all for them.
 *
 * Percent, not koruny: normalising by the base price is what lets a 400k cut on
 * a panelák and a 4M cut on a villa be compared at all. */
const ACCESSORS: Record<PipelineSortField, Accessor<PipelineBoardCard>> = {
  board_position: (c) => c.board_position,
  added_at: (c) => timeKey(c.added_at),
  entered_stage_at: (c) => timeKey(c.entered_stage_at),
  price_czk: (c) => c.price_czk,
  total_price_change_pct: (c) => c.total_price_change_pct,
  city: cityKey,
};

/* property_id is unique per card, so this is a total order — equal keys can
 * never reshuffle between refetches. */
export const sortPipelineCards = makeSorter<PipelineBoardCard, PipelineSortField>(
  ACCESSORS,
  byNumber((c) => c.property_id),
);
