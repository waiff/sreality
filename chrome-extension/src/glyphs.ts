/* The marks and stage rules every extension surface draws — the detail panel
 * and the search-page card controls — hand-reproduced from the SPA, which the
 * extension can't import (separate territory, classic content script). ONE
 * copy inside the extension, so the panel and a card never drift apart. */

import type { ExtCollection, PipelineStage } from './types';

/* The SPA's <FunnelIcon> (icons.tsx) as inline SVG — the shared "pipeline"
 * glyph on every surface (a funnel with three arrows; filled body =
 * in-pipeline). */
export function funnelIconSvg(filled: boolean): string {
  const f = filled ? 'currentColor' : 'none';
  return (
    '<svg class="pipeline-icon" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="1.75" stroke-linecap="round" ' +
    'stroke-linejoin="round" aria-hidden="true">' +
    '<line x1="7.5" y1="2" x2="7.5" y2="5.5"/><polyline points="6.2,4 7.5,5.7 8.8,4"/>' +
    '<line x1="12" y1="2" x2="12" y2="5.5"/><polyline points="10.7,4 12,5.7 13.3,4"/>' +
    '<line x1="16.5" y1="2" x2="16.5" y2="5.5"/><polyline points="15.2,4 16.5,5.7 17.8,4"/>' +
    `<path d="M4 8 H20 L13.5 15 V21 H10.5 V15 Z" fill="${f}"/></svg>`
  );
}

/* The SPA's <CollectionMark> (a bookmark; filled = in at least one collection)
 * — the collection glyph on every surface, and deliberately not the funnel:
 * collections are many-to-many groupings, the pipeline is the one deal state
 * (rule #22), and the two must never look alike. */
export function bookmarkIconSvg(filled: boolean): string {
  const f = filled ? 'currentColor' : 'none';
  return (
    `<svg class="collection-icon" viewBox="0 0 16 16" fill="${f}" ` +
    'stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M4 2.5 H12 V13.5 L8 10.75 L4 13.5 Z" stroke-linecap="round"/></svg>'
  );
}

/* The bell that marks a MONITORED collection in the checklist (the SPA menu's
 * BellGlyph): membership there is what turns into change alerts. */
export function bellIconSvg(): string {
  return (
    '<svg class="coll-bell-icon" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="1.75" stroke-linecap="round" ' +
    'stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M6 9 a6 6 0 0 1 12 0 c0 5 1.5 6.5 2.5 7.5 H3.5 C4.5 15.5 6 14 6 9 Z"/>' +
    '<path d="M10 20 a2 2 0 0 0 4 0"/></svg>'
  );
}

/* The SPA's <EyeOffIcon> (icons.tsx). `filled` = dismissed. */
export function eyeOffIconSvg(filled: boolean): string {
  const fill = filled ? ' fill="currentColor" fill-opacity="0.25"' : '';
  return (
    '<svg class="collection-icon" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="1.75" stroke-linecap="round" ' +
    'stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M2.5 12 C5 7.5 8.3 5.5 12 5.5 S19 7.5 21.5 12 C19 16.5 15.7 18.5 12 18.5 ' +
    `S5 16.5 2.5 12 Z"${fill}/>` +
    '<circle cx="12" cy="12" r="3"/><line x1="4" y1="20" x2="20" y2="4"/></svg>'
  );
}

/* The stage's accent, mirroring the SPA's lib/pipelineStage.ts:stageAccent —
 * the operator's stage colour, copper when the stage has none (copper is THE
 * deal-tracking accent, rule #22). Values come from tokens.css's --tag-* vars,
 * which mirror the SPA palette by value. */
const STAGE_COLORS = new Set([
  'copper', 'sage', 'brick', 'ochre', 'slate', 'plum', 'teal', 'sand',
]);

export function stageAccent(color: string | null | undefined): { fg: string; soft: string } {
  return color && STAGE_COLORS.has(color)
    ? { fg: `var(--tag-${color})`, soft: `var(--tag-${color}-soft)` }
    : { fg: 'var(--copper)', soft: 'var(--copper-soft)' };
}

/* The funnel badge, mirroring lib/pipelineStage.ts:stageBadge — the operator's
 * own short code, else the stage's 1-based ordinal among the live stages, else
 * nothing (stage list not loaded / stage archived). Never derived from
 * `position`: the live board reuses "9" across its three closed stages. */
export function stageBadge(
  code: string | null | undefined,
  stageId: number | null | undefined,
  stages: PipelineStage[] | null,
): string | null {
  if (code) return code;
  if (stageId == null || stages == null) return null;
  const idx = stages.findIndex((s) => s.id === stageId);
  return idx < 0 ? null : String(idx + 1);
}

/* The save control's name and its checklist's, in the SPA's words
 * (CollectionSaveMenu's COLLECTION_SAVE_LABEL) — one verb on every surface. */
export const COLLECTION_SAVE_LABEL = 'Uložit do kolekce';

/* The checklist's order, the SPA menu's: monitored collections first, then by
 * name. Never the server's order — GET /collections sorts by updated_at, which
 * every add/remove bumps, so the row just clicked would jump to the top. */
export function sortCollections(collections: ExtCollection[]): ExtCollection[] {
  return [...collections].sort(
    (a, b) =>
      Number(b.monitoring_enabled) - Number(a.monitoring_enabled)
      || a.name.localeCompare(b.name),
  );
}
