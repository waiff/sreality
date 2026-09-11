/* How big the deal-pipeline board draws its cards — one operator preference,
 * one geometry table.
 *
 * Three steps rather than the usual small/large pair, because the board's two
 * jobs pull apart: scanning a 40-card stage wants the dense row, judging a
 * property wants a photo you can actually read. So:
 *
 *   sm  the card as it has always been — a 3rem thumbnail beside the text.
 *   md  the SAME design with the thumbnail at exactly double, 6rem. Doubling
 *       the linear size is the convention Browse's --card-min already follows
 *       (11.5rem → 23rem), so "one step up" means the same thing app-wide.
 *   lg  a DIFFERENT design: the photo moves above the text and spans the card,
 *       the Browse-card idiom. Fewer cards per screen, but the photo is the
 *       point of this step.
 *
 * Every number a step changes lives in this one table, so a size can never be
 * half-applied — a wider column with the old thumbnail, a bigger card with the
 * old drop-zone floor. The page reads geometry, never a size literal. */

import { usePersistedChoice, type PersistedChoice } from '@/lib/persistedFlag';

export const PIPELINE_CARD_SIZES = ['sm', 'md', 'lg'] as const;
export type PipelineCardSize = (typeof PIPELINE_CARD_SIZES)[number];

export interface PipelineCardGeometry {
  /* The stage column's width. Widening is the cost of a bigger photo — the
   * board already scrolls horizontally at seven stages, so this trades screen
   * width the operator has (wide monitor) for photo the card didn't. */
  column: string;
  /* The photo frame. sm/md are squares beside the text, md exactly double sm;
   * lg is an aspect ratio because the frame is now as wide as the card. */
  thumb: string;
  /* lg stacks the photo above the text; sm/md keep them side by side. */
  stacked: boolean;
  /* Floor for the stage's drop zone. Columns stretch to the tallest one
   * (#1399), so this only binds when the WHOLE board is short — but then it
   * must still be a target, not a sliver. Three card rows at sm and md; lg's
   * literal three rows would be ~66rem, taller than any viewport and mostly
   * empty air on a sparse board, so it stops at the point where the target is
   * already unmistakable. */
  dropZoneMin: string;
  /* The drag ghost, a touch narrower than the column it came from. */
  overlay: string;
  /* One skeleton placeholder ≈ one card of this size, so the loading board has
   * the shape the real one will take. sm keeps the height the skeleton has
   * always drawn rather than being re-measured here — this table is about the
   * two new steps, not about restyling the default board's loading state. */
  skeletonRow: string;
}

export const PIPELINE_CARD_GEOMETRY: Record<PipelineCardSize, PipelineCardGeometry> = {
  sm: {
    column: 'w-72',
    thumb: 'h-12 w-12 shrink-0',
    stacked: false,
    dropZoneMin: 'min-h-[18rem]',
    overlay: 'w-64',
    skeletonRow: 'h-[4.5rem]',
  },
  md: {
    /* 24rem, not the 21rem the doubled thumbnail alone needs: at 21-22rem every
     * gained pixel went to the photo and the text column stayed exactly as
     * cramped as sm's. It still truncates a long per-m² beside the MF badge —
     * that is sm's own layout, and md is the SAME design with a bigger photo;
     * the step that gives the text a full line is lg, where the photo leaves
     * the row entirely. */
    column: 'w-[24rem]',
    thumb: 'h-24 w-24 shrink-0',
    stacked: false,
    dropZoneMin: 'min-h-[30rem]',
    overlay: 'w-[22rem]',
    skeletonRow: 'h-[9.5rem]',
  },
  lg: {
    column: 'w-[26rem]',
    thumb: 'aspect-[16/10] w-full',
    stacked: true,
    dropZoneMin: 'min-h-[32rem]',
    overlay: 'w-[24rem]',
    skeletonRow: 'h-[22rem]',
  },
};

/* Czech, like the rest of this page's chrome (Stav / Typ / Lokalita / Řazení).
 * The titles say what the step does to the BOARD, not to the card — the cost of
 * a bigger photo is how much of the pipeline you still see at once. */
export const PIPELINE_CARD_SIZE_LABELS: Record<
  PipelineCardSize,
  { label: string; title: string }
> = {
  sm: { label: 'Malé', title: 'Malý náhled vedle textu — nejvíc karet na obrazovku' },
  md: { label: 'Střední', title: 'Dvojnásobný náhled vedle textu' },
  lg: { label: 'Velké', title: 'Velká fotka nad textem, širší sloupce' },
};

const CARD_SIZE_KEY = 'sreality.pipeline.cardSize';

/* Default 'sm': the board every operator already knows. A preference this
 * strong about screen real estate is the operator's to opt into. */
export const usePipelineCardSize = (): PersistedChoice<PipelineCardSize> =>
  usePersistedChoice(CARD_SIZE_KEY, PIPELINE_CARD_SIZES, 'sm');
