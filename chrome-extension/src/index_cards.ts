/* The controls the extension draws ON a search-page card — the Browse card's
 * own cluster, in the same order: the pipeline funnel, the save-to-collection
 * bookmark and the hide eye, then the yield badge (sale apartments only).
 *
 * Every listing we have a property for gets the three controls, whatever its
 * category; they mean exactly what they mean in the app and write through the
 * same routes:
 *   funnel   — out of the pipeline: one click adds at the entry stage. In it:
 *              a click opens the stage menu (move, or remove behind a two-step
 *              confirm). Never a remove toggle (rule #22).
 *   bookmark — opens the checklist of every collection (rule #18); a click on
 *              a row adds or removes the property.
 *   eye      — hides the property / restores it, one click either way
 *              (migration 536). Absent while the property is a LIVE deal.
 *
 * Operator state is PROPERTY-grain: a write repaints every card of that
 * property on the page, and the panel when it shows the same property.
 *
 * The cluster lives in a closed shadow root per card, so the portal's CSS
 * can't reach it; the two menus share one page-level shadow host, positioned
 * against the button that opened them (a card's own overflow would clip a
 * menu drawn inside it). The only things written into the portal's DOM are
 * the host element and two attributes on the card. */

import tokens from './tokens.css?inline';
import {
  COLLECTION_SAVE_LABEL,
  bellIconSvg,
  bookmarkIconSvg,
  eyeOffIconSvg,
  funnelIconSvg,
  sortCollections,
  stageAccent,
  stageBadge,
} from './glyphs';
import { detailRef, type PortalRef } from './portals';
import type {
  ApiMessage,
  ApiResult,
  CollectionWriteResult,
  DismissalWriteResult,
  ExtCollection,
  PipelineCardResult,
  PipelineMembership,
  PipelineStage,
  PortalListing,
} from './types';

type Caller = <T>(m: ApiMessage) => Promise<ApiResult<T>>;

/* Holds the listing id the card was mounted FOR, not a boolean: SPA routers
 * recycle card DOM nodes between result sets, so a node can still carry the
 * controls of the listing it previously held. Storing the id lets a recycled
 * node be detected and re-mounted (or, while its lookup is failing, cleared)
 * instead of silently showing — and writing to — another listing's property. */
export const PROCESSED_ATTR = 'data-mf-processed';
/* On <html>: "1" while dismissed properties are drawn barely visible. */
export const VEIL_ROOT_ATTR = 'data-mf-veil';
/* On a card whose property the operator dismissed. */
const DISMISSED_ATTR = 'data-mf-dismissed';
const HOST_CLASS = '__mf_card_host';
const MENU_HOST_ID = '__mf_menu_host__';
const PAGE_STYLE_ID = '__mf_card_style__';
const STAGE_MENU_LABEL = 'Fáze v pipeline';
const LIST_RETRY_MS = 1500;

export interface BadgeView {
  kind: 'yield' | 'est' | 'cta';
  text: string;
  title: string;
}

export interface CardLayerDeps {
  call: Caller;
  /* The overlay's lookup results by native id — the one store. Entries are
   * replaced, never mutated. */
  cache: Map<string, PortalListing>;
  /* The yield badge for this card, or null when it gets none. */
  badgeFor: (listing: PortalListing, href: string) => BadgeView | null;
  openPanel: (ref: PortalRef, href: string, listing: PortalListing) => void;
  /* The app's collections page, for the checklist's manage link; null hides it. */
  collectionsUrl: string | null;
  /* Whether dismissed properties are drawn barely visible right now. */
  veilOn: () => boolean;
  /* An API failure detail in the operator's words. */
  describe: (detail: string) => string;
  /* Say why a write failed (the dock's line). */
  flash: (message: string) => void;
  /* A write made here settled: this is the property's state now. */
  onWrite: (listing: PortalListing) => void;
  /* Cards were repainted — the dismissed count may have moved. */
  onRefresh: () => void;
}

export interface CardLayer {
  /* Draw the controls for the listing this card links (idempotent per id). */
  mount: (card: HTMLElement, ref: PortalRef, href: string) => void;
  clear: (card: HTMLElement) => void;
  /* Repaint every card and the open menu from the store. */
  refresh: () => void;
  dismissedCount: () => number;
  /* Drop every control and everything loaded: the overlay stopped, or the
   * session changed and what is held belongs to the account that left. */
  reset: () => void;
}

type MenuKind = 'stages' | 'collections';

interface Menu {
  kind: MenuKind;
  propertyId: number;
  anchor: HTMLButtonElement;
  /* The card host the anchor lives in — what a pointer event on it targets. */
  anchorHost: HTMLElement;
  /* The stage menu is showing its "Odebrat z pipeline?" confirm. */
  confirming: boolean;
  /* The last collection write's failure, shown beside the rows. */
  error: string | null;
}

interface MenuDom {
  host: HTMLElement;
  shadow: ShadowRoot;
  box: HTMLElement;
}

interface CardHandle {
  sourceId: string;
  paint: () => void;
  destroy: () => void;
}

const OUT_OF_PIPELINE: PipelineMembership = {
  in_pipeline: false, stage_id: null, stage_key: null, stage_label: null,
  stage_code: null, stage_color: null,
};

/* In the portal's DOM, so it only ever names our own attributes. Dimming the
 * card's CHILDREN rather than the card keeps our own controls legible on it —
 * the eye is how the operator takes the dismissal back — and needs no guess at
 * the page's background colour. Hovering the card lifts it enough to see what
 * it is before restoring it. */
const PAGE_CSS = `
  html[${VEIL_ROOT_ATTR}="1"] [${DISMISSED_ATTR}] > :not(.${HOST_CLASS}) {
    opacity: 0.1 !important; filter: grayscale(1) !important;
    transition: opacity 140ms ease !important;
  }
  html[${VEIL_ROOT_ATTR}="1"] [${DISMISSED_ATTR}]:hover > :not(.${HOST_CLASS}) {
    opacity: 0.5 !important;
  }
`;

/* `!important` on the host's own box: inside a shadow tree it is what lets
 * :host beat a portal rule that happens to match our element (".card > div").
 *
 * z-index 900, not the maximum: a card rarely makes a stacking context of its
 * own, so this competes with the whole page. It has to clear what a card
 * stacks inside itself (stretched links, carousel arrows: single and double
 * digits) and stay UNDER what a portal floats over its results — sticky
 * headers, filter drawers, galleries, cookie walls (the common scales start at
 * 1000). At the maximum the controls showed through every one of those. */
const CARD_CSS = `
  ${tokens}
  :host {
    all: initial !important;
    position: absolute !important; top: 6px !important; left: 6px !important;
    z-index: 900 !important; display: block !important;
  }
  [hidden] { display: none !important; }
  .bar {
    display: flex; align-items: center; gap: 3px;
    font-family: system-ui, -apple-system, sans-serif;
    transition: opacity 140ms ease;
  }
  :host([data-veiled]) .bar { opacity: 0.5; }
  :host-context([${DISMISSED_ATTR}]:hover) .bar,
  :host([data-veiled]) .bar:focus-within { opacity: 1; }
  .act {
    box-sizing: border-box; display: inline-flex; align-items: center;
    justify-content: center; gap: 2px; height: 24px; min-width: 24px;
    margin: 0; padding: 0; font: inherit; color: var(--ink-3);
    background: rgba(255, 253, 248, 0.92); border: 1px solid rgba(28, 28, 28, 0.32);
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.12);
    -webkit-backdrop-filter: blur(4px); backdrop-filter: blur(4px);
    cursor: pointer;
    transition: color 120ms ease, border-color 120ms ease, background 120ms ease;
  }
  .act:hover { color: var(--copper); border-color: var(--copper); }
  .act--dismiss:hover { color: var(--ink); border-color: var(--ink); }
  .act[aria-disabled="true"] { opacity: 0.6; cursor: wait; }
  .act:focus-visible, .badge:focus-visible { outline: 2px solid var(--copper); outline-offset: 1px; }
  .act svg { width: 14px; height: 14px; flex-shrink: 0; }
  .act--in { padding: 0 4px; background: rgba(255, 253, 248, 0.96); }
  .act--saved { color: var(--copper); border-color: var(--copper); background: var(--copper-soft); }
  .act--on { color: var(--ink); border-color: var(--ink-3); background: var(--bg-sunk); }
  .code {
    font-size: 10px; font-weight: 600; line-height: 1;
    font-variant-numeric: tabular-nums;
  }
  .badge {
    box-sizing: border-box; display: inline-flex; align-items: center;
    height: 24px; margin: 0; padding: 0 7px;
    font: inherit; font-size: 11px; font-weight: 600; line-height: 1;
    letter-spacing: 0.02em; font-variant-numeric: tabular-nums; white-space: nowrap;
    border: 1px solid var(--rule-strong); box-shadow: 0 1px 3px rgba(0, 0, 0, 0.12);
    cursor: pointer;
  }
  .badge--yield { background: var(--copper); color: #fff; }
  .badge--est { background: var(--ink-3); color: #fff; }
  .badge--cta { background: var(--bg); color: var(--copper); }
  .badge--cta:hover { background: var(--copper-soft); }
  .badge--yield:hover, .badge--est:hover { filter: brightness(1.08); }
  @media (prefers-reduced-motion: reduce) { .bar, .act { transition: none; } }
`;

/* The panel's slip, floated: paper surface, ink edge, the copper "filed" top
 * edge. Rows are the panel checklist's rows. */
const MENU_CSS = `
  ${tokens}
  :host { all: initial; }
  [hidden] { display: none !important; }
  .menu {
    position: fixed; top: 0; left: 0; z-index: 2147483647;
    box-sizing: border-box; width: 248px; max-width: calc(100vw - 16px);
    max-height: calc(100vh - 16px); overflow-y: auto; overscroll-behavior: contain;
    padding: 8px 6px;
    font-family: 'Inter', system-ui, -apple-system, sans-serif;
    font-size: 13px; line-height: 1.3; text-align: left; color: var(--ink);
    background: var(--bg); border: 1px solid var(--rule-strong);
    box-shadow: inset 0 2px 0 var(--copper), 0 10px 34px -10px rgba(28, 20, 10, 0.35);
  }
  .menu:focus { outline: none; }
  .eyebrow {
    margin: 2px 6px 6px; font-size: 10px; text-transform: uppercase;
    letter-spacing: 0.16em; color: var(--ink-3);
  }
  .list {
    display: flex; flex-direction: column; gap: 1px; max-height: 240px;
    margin: 0; padding: 0; overflow-y: auto; list-style: none;
  }
  .row {
    box-sizing: border-box; display: flex; align-items: center; gap: 8px;
    width: 100%; margin: 0; padding: 6px; font: inherit; text-align: left;
    color: var(--ink); background: transparent; border: 0; cursor: pointer;
    transition: background 120ms ease;
  }
  .row:hover:not([aria-disabled="true"]) { background: var(--copper-soft); }
  .row[aria-disabled="true"] { opacity: 0.6; cursor: wait; }
  .row:focus-visible, .link:focus-visible, .yes:focus-visible, .no:focus-visible {
    outline: 2px solid var(--copper); outline-offset: -2px;
  }
  .name {
    flex: 1 1 auto; min-width: 0; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap;
  }
  .stage-code {
    box-sizing: border-box; display: inline-flex; align-items: center;
    justify-content: center; min-width: 18px; height: 16px; padding: 0 3px;
    flex-shrink: 0; font-size: 10px; font-weight: 600; line-height: 1;
    font-variant-numeric: tabular-nums; border: 1px solid currentColor;
  }
  .mark { flex-shrink: 0; font-size: 11px; }
  .check {
    box-sizing: border-box; display: inline-flex; align-items: center;
    justify-content: center; width: 14px; height: 14px; flex-shrink: 0;
    font-size: 10px; line-height: 1; color: transparent;
    background: var(--paper); border: 1px solid var(--ink-4);
  }
  .row--member .check { color: #fff; background: var(--copper); border-color: var(--copper); }
  .bell { display: inline-flex; flex-shrink: 0; color: var(--copper); }
  .bell svg { width: 13px; height: 13px; }
  .state { margin: 2px 6px; font-size: 12px; color: var(--ink-3); }
  .error { margin: 6px 6px 0; font-size: 11px; line-height: 1.35; color: #b34730; }
  .link {
    margin: 0; padding: 0; font: inherit; font-weight: 600; color: var(--copper);
    text-decoration: none; background: transparent; border: 0; cursor: pointer;
  }
  .link:hover { color: var(--copper-deep); text-decoration: underline; }
  .manage {
    display: inline-block; margin: 8px 6px 0; font-size: 10px;
    text-transform: uppercase; letter-spacing: 0.12em;
  }
  .sep { height: 0; margin: 6px 0 4px; border: 0; border-top: 1px solid var(--rule); }
  .row--remove { color: var(--ink-3); }
  .row--remove:hover:not([aria-disabled="true"]) { color: #b34730; background: rgba(179, 71, 48, 0.08); }
  .confirm { padding: 4px 6px 2px; }
  .confirm-q { margin: 0; font-size: 12px; font-weight: 600; }
  .confirm-d { margin: 3px 0 0; font-size: 11px; line-height: 1.35; color: var(--ink-3); }
  .confirm-actions { display: flex; gap: 6px; margin-top: 7px; }
  .yes, .no {
    margin: 0; padding: 3px 8px; font: inherit; font-size: 11px;
    background: transparent; cursor: pointer;
  }
  .yes { color: #b34730; border: 1px solid #b34730; }
  .yes:hover { color: var(--paper); background: #b34730; }
  .no { color: var(--ink-3); border: 1px solid var(--rule); }
  .no:hover { color: var(--ink); border-color: var(--ink-4); }
`;

/* Events that start inside the cluster end there. A card is often one big
 * link (or a carousel that drags on pointer-down), and SPA routers listen at
 * the document: a click on the funnel must neither navigate nor reach them. */
const SHIELDED_EVENTS = [
  'click', 'auxclick', 'dblclick', 'mousedown', 'mouseup', 'pointerdown',
  'pointerup', 'touchstart', 'touchend', 'keydown', 'keyup', 'keypress',
] as const;

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K, className: string, text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function button(className: string, text?: string): HTMLButtonElement {
  const b = el('button', className, text);
  b.type = 'button';
  return b;
}

function setBusy(node: HTMLElement, busy: boolean): void {
  /* aria-disabled, never `disabled`: a disabled button drops keyboard focus to
   * the portal page for the length of the write. Handlers ignore busy clicks. */
  if (busy) node.setAttribute('aria-disabled', 'true');
  else node.removeAttribute('aria-disabled');
}

function injectPageStyle(): void {
  if (document.getElementById(PAGE_STYLE_ID) != null) return;
  const style = document.createElement('style');
  style.id = PAGE_STYLE_ID;
  style.textContent = PAGE_CSS;
  (document.head ?? document.documentElement).appendChild(style);
}

/* True when every listing link inside the node is this listing's. The card is
 * found by a markup-agnostic guess (index_overlay's cardFor); should that guess
 * land on a wrapper holding several results, dimming it would blank them all —
 * so such a node is never dimmed, and never climbed to. */
export function holdsOneListing(card: HTMLElement, sourceId: string): boolean {
  return Array.from(card.querySelectorAll<HTMLAnchorElement>('a[href]')).every((a) => {
    const ref = detailRef(a.href, location.hostname);
    return ref == null || ref.sourceId === sourceId;
  });
}

export function createCardLayer(deps: CardLayerDeps): CardLayer {
  const { call, cache } = deps;
  injectPageStyle();

  const handles = new Map<HTMLElement, CardHandle>();
  /* `p:` pipeline, `d:` dismissal, `c:` collection — one write of a kind per
   * property at a time. */
  const busy = new Set<string>();
  let epoch = 0;  // bumped by reset(): answers to older asks are dropped

  let stages: PipelineStage[] | null = null;
  let stagesLoading = false;
  let stagesFailed = false;
  let collections: ExtCollection[] | null = null;
  let collectionsLoading = false;
  let collectionsFailed = false;

  let menu: Menu | null = null;
  let menuDom: MenuDom | null = null;
  /* What the open menu was last built from. refresh() runs on every overlay
   * pass — any DOM mutation of the portal page — and a menu rebuilt under the
   * pointer swallows the click being made, so it is rebuilt only on a change. */
  let menuPainted = '';

  function listingOf(propertyId: number): PortalListing | null {
    for (const l of cache.values()) if (l.property_id === propertyId) return l;
    return null;
  }

  /* Operator state is property-grain: every advert of the property on this
   * page takes the change. */
  function patch(propertyId: number, change: (l: PortalListing) => PortalListing): void {
    for (const [id, l] of cache) {
      if (l.property_id === propertyId) cache.set(id, change(l));
    }
    refresh();
  }

  function settled(propertyId: number): void {
    const l = listingOf(propertyId);
    if (l != null) deps.onWrite(l);
  }

  /* A LIVE deal and a dismissal never coexist (the API refuses it). Liveness is
   * read off the stage list — the one place `is_terminal` lives; until it has
   * loaded an in-pipeline card counts as live, the server's own answer for one. */
  function isLive(l: PortalListing): boolean {
    const p = l.pipeline;
    if (!p?.in_pipeline) return false;
    const stage = stages?.find((s) => s.id === p.stage_id);
    return stage == null || !stage.is_terminal;
  }

  /* Bounded retry, like the panel's list reads: one network blip must not
   * strip stage-changing for the page view. */
  async function readList<T>(message: ApiMessage, asked: number): Promise<T | null> {
    for (let attempt = 0; attempt < 3; attempt++) {
      const res = await call<T>(message);
      if (asked !== epoch) return null;
      if (res.ok) return res.data;
      if (attempt < 2) {
        await sleep(LIST_RETRY_MS);
        if (asked !== epoch) return null;
      }
    }
    return null;
  }

  async function loadStages(): Promise<void> {
    if (stages != null || stagesLoading) return;
    stagesLoading = true;
    const hadFailed = stagesFailed;
    stagesFailed = false;
    if (hadFailed) refresh();
    const asked = epoch;
    const data = await readList<PipelineStage[]>({ type: 'list_pipeline_stages' }, asked);
    if (asked !== epoch) return;
    stagesLoading = false;
    if (data == null) stagesFailed = true;
    else stages = data;
    refresh();
  }

  /* `reread` re-reads a list already held: opening the checklist does, so a
   * collection made in the app since the page loaded shows up. */
  async function loadCollections(reread: boolean): Promise<void> {
    if (collectionsLoading || (collections != null && !reread)) return;
    collectionsLoading = true;
    const hadFailed = collectionsFailed;
    collectionsFailed = false;
    if (hadFailed) refresh();
    const asked = epoch;
    const data = await readList<ExtCollection[]>({ type: 'list_collections' }, asked);
    if (asked !== epoch) return;
    collectionsLoading = false;
    if (data == null) {
      // A failed re-read keeps the list it already had; only an empty menu fails.
      if (collections != null) return;
      collectionsFailed = true;
      refresh();
      return;
    }
    /* The usual re-read changes nothing and lands just as the checklist opened —
     * rebuilding it then, under the pointer, can swallow the click being made. */
    const key = (list: ExtCollection[]): string =>
      JSON.stringify(sortCollections(list).map((c) => [c.id, c.name, c.monitoring_enabled]));
    const changed = collections == null || key(collections) !== key(data);
    collections = data;
    if (changed) refresh();
  }

  // ---- writes ---------------------------------------------------------------
  // Optimistic, then reconciled from the server's answer; a failure puts the
  // prior state back and says why. All through the SAME routes the app uses.

  async function addToPipeline(propertyId: number): Promise<void> {
    const key = `p:${propertyId}`;
    if (busy.has(key)) return;
    const asked = epoch;
    busy.add(key);
    patch(propertyId, (l) => ({ ...l, pipeline: { ...OUT_OF_PIPELINE, in_pipeline: true } }));
    const res = await call<PipelineCardResult>({ type: 'add_pipeline_card', property_id: propertyId });
    if (asked !== epoch) return;
    busy.delete(key);
    if (!res.ok) {
      patch(propertyId, (l) => ({ ...l, pipeline: OUT_OF_PIPELINE }));
      deps.flash(`Uložení do pipeline selhalo: ${deps.describe(res.detail)}`);
      return;
    }
    const card = res.data;
    /* A new card is live (the entry stage can't be terminal), and a live deal
     * lifts the caller's dismissal server-side — mirror it. */
    patch(propertyId, (l) => ({
      ...l,
      pipeline: {
        in_pipeline: true,
        stage_id: card.stage_id ?? null,
        stage_key: card.stage_key ?? null,
        stage_label: card.stage_label ?? null,
        stage_code: card.stage_code ?? null,
        stage_color: card.stage_color ?? null,
      },
      dismissed: l.dismissed == null ? l.dismissed : false,
    }));
    settled(propertyId);
  }

  async function moveStage(propertyId: number, stageId: number): Promise<void> {
    const key = `p:${propertyId}`;
    const prior = listingOf(propertyId)?.pipeline;
    if (busy.has(key) || prior == null || prior.stage_id === stageId) return;
    const target = stages?.find((s) => s.id === stageId) ?? null;
    const asked = epoch;
    busy.add(key);
    patch(propertyId, (l) => ({
      ...l,
      pipeline: {
        in_pipeline: true, stage_id: stageId,
        stage_key: target?.key ?? prior.stage_key,
        stage_label: target?.label ?? prior.stage_label,
        stage_code: target?.code ?? null,
        stage_color: target?.color ?? prior.stage_color,
      },
    }));
    const res = await call<PipelineCardResult>({
      type: 'move_pipeline_card', property_id: propertyId, stage_id: stageId,
    });
    if (asked !== epoch) return;
    busy.delete(key);
    if (!res.ok) {
      patch(propertyId, (l) => ({ ...l, pipeline: prior }));
      deps.flash(`Změna fáze selhala: ${deps.describe(res.detail)}`);
      return;
    }
    const card = res.data;
    // Re-opening a closed deal makes it live, which lifts its dismissal server-side.
    const reopened = target != null && !target.is_terminal;
    patch(propertyId, (l) => ({
      ...l,
      pipeline: {
        in_pipeline: true,
        stage_id: card.stage_id ?? stageId,
        stage_key: card.stage_key ?? target?.key ?? prior.stage_key,
        stage_label: card.stage_label ?? target?.label ?? prior.stage_label,
        stage_code: card.stage_code ?? target?.code ?? null,
        stage_color: card.stage_color ?? target?.color ?? prior.stage_color,
      },
      dismissed: reopened && l.dismissed != null ? false : l.dismissed,
    }));
    settled(propertyId);
  }

  async function removeFromPipeline(propertyId: number): Promise<void> {
    const key = `p:${propertyId}`;
    const prior = listingOf(propertyId)?.pipeline;
    if (busy.has(key) || prior == null || !prior.in_pipeline) return;
    const asked = epoch;
    busy.add(key);
    patch(propertyId, (l) => ({ ...l, pipeline: OUT_OF_PIPELINE }));
    const res = await call<PipelineCardResult>({
      type: 'remove_pipeline_card', property_id: propertyId,
    });
    if (asked !== epoch) return;
    busy.delete(key);
    if (!res.ok) {
      patch(propertyId, (l) => ({ ...l, pipeline: prior }));
      deps.flash(`Odebrání z pipeline selhalo: ${deps.describe(res.detail)}`);
      return;
    }
    refresh();
    settled(propertyId);
  }

  async function toggleDismiss(propertyId: number): Promise<void> {
    const key = `d:${propertyId}`;
    const wasOn = listingOf(propertyId)?.dismissed;
    if (busy.has(key) || wasOn == null) return;
    const asked = epoch;
    busy.add(key);
    patch(propertyId, (l) => ({ ...l, dismissed: !wasOn }));
    const res = await call<DismissalWriteResult>({
      type: wasOn ? 'undismiss_property' : 'dismiss_property', property_id: propertyId,
    });
    if (asked !== epoch) return;
    busy.delete(key);
    if (!res.ok) {
      patch(propertyId, (l) => ({ ...l, dismissed: wasOn }));
      deps.flash(`Skrytí se nepodařilo uložit: ${deps.describe(res.detail)}`);
      return;
    }
    refresh();
    settled(propertyId);
  }

  function withMembership(l: PortalListing, collectionId: number, member: boolean): PortalListing {
    const ids = l.collection_ids ?? [];
    if (ids.includes(collectionId) === member) return l;
    return {
      ...l,
      collection_ids: member ? [...ids, collectionId] : ids.filter((id) => id !== collectionId),
    };
  }

  async function toggleCollection(propertyId: number, collectionId: number): Promise<void> {
    const key = `c:${propertyId}`;
    const l = listingOf(propertyId);
    if (busy.has(key) || l == null) return;
    const add = !(l.collection_ids ?? []).includes(collectionId);
    const asked = epoch;
    busy.add(key);
    if (menu?.propertyId === propertyId) menu.error = null;
    patch(propertyId, (row) => withMembership(row, collectionId, add));
    const res = await call<CollectionWriteResult>({
      type: add ? 'add_to_collection' : 'remove_from_collection',
      collection_id: collectionId,
      property_id: propertyId,
    });
    if (asked !== epoch) return;
    busy.delete(key);
    if (!res.ok) {
      const message = add
        ? `Uložení do kolekce se nepodařilo: ${deps.describe(res.detail)}`
        : `Odebrání z kolekce se nepodařilo: ${deps.describe(res.detail)}`;
      /* Say it where it can be seen: beside the rows while the checklist is
       * still open for this property, else on the dock. */
      if (menu?.kind === 'collections' && menu.propertyId === propertyId) menu.error = message;
      else deps.flash(message);
      patch(propertyId, (row) => withMembership(row, collectionId, !add));
      /* The likeliest cause is a collection deleted in the app since the list
       * loaded (a 404) — re-read it so the dead row goes away. */
      void loadCollections(true);
      return;
    }
    refresh();
    settled(propertyId);
  }

  // ---- the card cluster -----------------------------------------------------

  function mount(card: HTMLElement, ref: PortalRef, href: string): void {
    const sourceId = ref.sourceId;
    if (card.getAttribute(PROCESSED_ATTR) === sourceId) return;
    clear(card);  // a recycled node drops the previous listing's controls first
    card.setAttribute(PROCESSED_ATTR, sourceId);

    const first = cache.get(sourceId);
    if (first == null) return;
    const hasProperty = first.found && first.property_id != null;
    if (!hasProperty && deps.badgeFor(first, href) == null) return;

    if (getComputedStyle(card).position === 'static') card.style.position = 'relative';

    const host = document.createElement('div');
    host.className = HOST_CLASS;
    const shadow = host.attachShadow({ mode: 'closed' });
    const style = document.createElement('style');
    style.textContent = CARD_CSS;
    const bar = el('div', 'bar');
    const funnel = button('act');
    const save = button('act');
    const hide = button('act act--dismiss');
    const badge = button('badge');
    bar.append(funnel, save, hide, badge);
    shadow.append(style, bar);

    const current = (): { listing: PortalListing; propertyId: number } | null => {
      const listing = cache.get(sourceId);
      return listing?.found && listing.property_id != null
        ? { listing, propertyId: listing.property_id }
        : null;
    };

    funnel.onclick = () => {
      const now = current();
      if (now == null || busy.has(`p:${now.propertyId}`)) return;
      if (now.listing.pipeline?.in_pipeline) toggleMenu('stages', now.propertyId, funnel, host);
      else {
        closeMenu(false);
        void addToPipeline(now.propertyId);
      }
    };
    save.onclick = () => {
      const now = current();
      if (now != null) toggleMenu('collections', now.propertyId, save, host);
    };
    hide.onclick = () => {
      const now = current();
      if (now == null) return;
      closeMenu(false);
      void toggleDismiss(now.propertyId);
    };
    badge.onclick = () => {
      const listing = cache.get(sourceId);
      if (listing == null) return;
      closeMenu(false);
      deps.openPanel(ref, href, listing);
    };

    for (const type of SHIELDED_EVENTS) {
      host.addEventListener(type, (e) => {
        e.stopPropagation();
        if (type === 'click' || type === 'auxclick') e.preventDefault();
        if (type === 'keydown' && (e as KeyboardEvent).key === 'Escape' && menu?.anchorHost === host) {
          closeMenu(true);
        }
      }, { passive: type === 'touchstart' || type === 'touchend' });
    }

    let painted = '';
    function paint(): void {
      const l = cache.get(sourceId);
      if (l == null) return;
      const pid = l.found ? l.property_id : null;
      const p = pid != null ? l.pipeline : null;
      const inPipe = p?.in_pipeline ?? false;
      const code = inPipe ? stageBadge(p?.stage_code, p?.stage_id, stages) : null;
      const saved = pid != null ? (l.collection_ids ?? []).length : 0;
      const dismissed = pid != null && l.dismissed === true;
      const canHide = pid != null && l.dismissed != null && !isLive(l);
      const veiled = dismissed && deps.veilOn();
      const view = deps.badgeFor(l, href);
      const state = JSON.stringify([
        pid, inPipe, p?.stage_label, p?.stage_color, code, saved, dismissed, canHide,
        veiled, view, busy.has(`p:${pid}`), busy.has(`d:${pid}`),
        menu?.anchor === funnel, menu?.anchor === save,
      ]);
      if (state === painted) return;
      painted = state;

      funnel.hidden = pid == null;
      funnel.className = 'act' + (inPipe ? ' act--in' : '');
      const accent = stageAccent(p?.stage_color);
      funnel.style.color = inPipe ? accent.fg : '';
      funnel.style.borderColor = inPipe ? accent.fg : '';
      const funnelLabel = !inPipe
        ? 'Přidat do pipeline'
        : p?.stage_label
          ? `V pipeline (${p.stage_label}) — změnit fázi`
          : 'V pipeline — změnit fázi';
      funnel.title = funnelLabel;
      funnel.setAttribute('aria-label', funnelLabel);
      funnel.setAttribute('aria-pressed', String(inPipe));
      if (inPipe) {
        funnel.setAttribute('aria-haspopup', 'menu');
        funnel.setAttribute('aria-expanded', String(menu?.anchor === funnel));
      } else {
        funnel.removeAttribute('aria-haspopup');
        funnel.removeAttribute('aria-expanded');
      }
      setBusy(funnel, busy.has(`p:${pid}`));
      funnel.innerHTML = funnelIconSvg(inPipe);
      if (code != null) funnel.appendChild(el('span', 'code', code));

      save.hidden = pid == null;
      save.className = 'act' + (saved > 0 ? ' act--saved' : '');
      const saveLabel = saved === 0
        ? COLLECTION_SAVE_LABEL
        : `${COLLECTION_SAVE_LABEL} — v ${saved} ${saved === 1 ? 'kolekci' : 'kolekcích'}`;
      save.title = saveLabel;
      save.setAttribute('aria-label', saveLabel);
      save.setAttribute('aria-expanded', String(menu?.anchor === save));
      save.innerHTML = bookmarkIconSvg(saved > 0);

      hide.hidden = !canHide;
      hide.className = 'act act--dismiss' + (dismissed ? ' act--on' : '');
      const hideLabel = dismissed ? 'Skryto — znovu zobrazit' : 'Skrýt nemovitost';
      hide.title = hideLabel;
      hide.setAttribute('aria-label', hideLabel);
      hide.setAttribute('aria-pressed', String(dismissed));
      setBusy(hide, busy.has(`d:${pid}`));
      hide.innerHTML = eyeOffIconSvg(dismissed);

      badge.hidden = view == null;
      if (view != null) {
        badge.className = `badge badge--${view.kind}`;
        badge.textContent = view.text;
        badge.title = view.title;
      }

      host.toggleAttribute('data-veiled', veiled);
      if (dismissed && holdsOneListing(card, sourceId)) card.setAttribute(DISMISSED_ATTR, '');
      else card.removeAttribute(DISMISSED_ATTR);
    }

    handles.set(card, {
      sourceId,
      paint,
      destroy(): void {
        if (menu?.anchorHost === host) closeMenu(false);
        host.remove();
      },
    });
    card.appendChild(host);
    paint();
    // The stage list names each card's badge and says which deals are live.
    if (hasProperty && !stagesFailed) void loadStages();
  }

  function clear(card: HTMLElement): void {
    const handle = handles.get(card);
    handles.delete(card);
    handle?.destroy();
    // A host left by an overlay run that has since stopped has no handle here.
    card.querySelector(`:scope > .${HOST_CLASS}`)?.remove();
    card.removeAttribute(PROCESSED_ATTR);
    card.removeAttribute(DISMISSED_ATTR);
  }

  function refresh(): void {
    for (const [card, handle] of [...handles]) {
      // The portal dropped the node; should it ever put it back, it re-mounts.
      if (!card.isConnected) clear(card);
      else handle.paint();
    }
    renderMenu();
    deps.onRefresh();
  }

  function dismissedCount(): number {
    const ids = new Set<string>();
    for (const [card, handle] of handles) {
      const l = cache.get(handle.sourceId);
      if (card.isConnected && l?.property_id != null && l.dismissed === true) {
        ids.add(handle.sourceId);
      }
    }
    return ids.size;
  }

  function reset(): void {
    epoch++;
    closeMenu(false);
    for (const card of [...handles.keys()]) clear(card);
    busy.clear();
    stages = null;
    stagesLoading = false;
    stagesFailed = false;
    collections = null;
    collectionsLoading = false;
    collectionsFailed = false;
  }

  // ---- the menus ------------------------------------------------------------

  function toggleMenu(
    kind: MenuKind, propertyId: number, anchor: HTMLButtonElement, anchorHost: HTMLElement,
  ): void {
    const reopen = menu?.anchor !== anchor;
    closeMenu(false);
    if (!reopen) return;

    document.getElementById(MENU_HOST_ID)?.remove();
    const host = document.createElement('div');
    host.id = MENU_HOST_ID;
    const shadow = host.attachShadow({ mode: 'closed' });
    const style = document.createElement('style');
    style.textContent = MENU_CSS;
    const box = el('div', 'menu');
    box.tabIndex = -1;
    box.addEventListener('keydown', onMenuKey);
    shadow.append(style, box);
    document.body.appendChild(host);

    menu = { kind, propertyId, anchor, anchorHost, confirming: false, error: null };
    menuDom = { host, shadow, box };
    menuPainted = '';
    document.addEventListener('pointerdown', onOutsidePointer, true);
    window.addEventListener('scroll', placeMenu, { capture: true, passive: true });
    window.addEventListener('resize', placeMenu);

    if (kind === 'stages') void loadStages();
    else void loadCollections(true);
    refresh();
    focusInMenu(kind === 'stages' ? '[aria-checked="true"]' : '.row');
  }

  /* `refocus` hands focus back to the control that opened the menu — Escape and
   * a made choice do; a click elsewhere has already put focus where it belongs. */
  function closeMenu(refocus: boolean): void {
    if (menu == null) return;
    const { anchor } = menu;
    menu = null;
    document.removeEventListener('pointerdown', onOutsidePointer, true);
    window.removeEventListener('scroll', placeMenu, true);
    window.removeEventListener('resize', placeMenu);
    menuDom?.host.remove();
    menuDom = null;
    if (refocus && anchor.isConnected) anchor.focus();
    refresh();  // the anchor's aria-expanded
  }

  /* Both shadow roots are closed, so a pointer event from inside either one
   * arrives here targeting its host. */
  function onOutsidePointer(e: Event): void {
    if (menu == null || menuDom == null) return;
    // The anchor's own click toggles the menu; closing it here would reopen it.
    if (e.target === menuDom.host || e.target === menu.anchorHost) return;
    closeMenu(false);
  }

  function onMenuKey(e: KeyboardEvent): void {
    e.stopPropagation();  // ours, not the portal's (lightboxes close on Escape too)
    if (menuDom == null) return;
    if (e.key === 'Escape') {
      e.preventDefault();
      closeMenu(true);
      return;
    }
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    const items = Array.from(
      menuDom.box.querySelectorAll<HTMLElement>('.row, .yes, .no'),
    );
    if (items.length === 0) return;
    e.preventDefault();
    const at = items.indexOf(menuDom.shadow.activeElement as HTMLElement);
    const down = e.key === 'ArrowDown';
    const next = at < 0
      ? (down ? 0 : items.length - 1)
      : (at + (down ? 1 : -1) + items.length) % items.length;
    items[next].focus();
  }

  function focusInMenu(selector: string): void {
    if (menuDom == null) return;
    const target = menuDom.box.querySelector<HTMLElement>(selector)
      ?? menuDom.box.querySelector<HTMLElement>('.row');
    (target ?? menuDom.box).focus({ preventScroll: true });
  }

  /* Under the anchor, flipped above it when the viewport runs out; a menu
   * whose anchor is gone (the portal re-rendered the card) closes. */
  function placeMenu(): void {
    if (menu == null || menuDom == null) return;
    if (!menu.anchor.isConnected) {
      closeMenu(false);
      return;
    }
    const { box } = menuDom;
    const r = menu.anchor.getBoundingClientRect();
    const width = box.offsetWidth;
    const height = box.offsetHeight;
    const left = Math.max(8, Math.min(r.left, window.innerWidth - width - 8));
    const below = r.bottom + 4;
    const above = r.top - 4 - height;
    const top = below + height > window.innerHeight - 8 && above >= 8 ? above : below;
    box.style.left = `${Math.round(left)}px`;
    box.style.top = `${Math.round(top)}px`;
  }

  function renderMenu(): void {
    if (menu == null || menuDom == null) return;
    const l = listingOf(menu.propertyId);
    if (
      !menu.anchor.isConnected || l == null
      || (menu.kind === 'stages' && !l.pipeline?.in_pipeline)
    ) {
      closeMenu(false);
      return;
    }
    const { box, shadow } = menuDom;
    const view = JSON.stringify([
      menu.kind, menu.propertyId, menu.confirming, menu.error,
      l.pipeline, l.collection_ids, busy.has(`p:${menu.propertyId}`), busy.has(`c:${menu.propertyId}`),
      stages, stagesFailed, collections, collectionsFailed,
    ]);
    if (view === menuPainted) {
      placeMenu();
      return;
    }
    menuPainted = view;
    const focusedKey = (shadow.activeElement as HTMLElement | null)?.dataset.key ?? null;
    const listTop = box.querySelector<HTMLElement>('.list')?.scrollTop ?? 0;
    box.replaceChildren();
    if (menu.kind === 'stages') buildStageMenu(box, l, menu);
    else buildCollectionMenu(box, l, menu);
    const list = box.querySelector<HTMLElement>('.list');
    if (list != null) list.scrollTop = listTop;
    if (focusedKey != null) {
      (box.querySelector<HTMLElement>(`[data-key="${focusedKey}"]`) ?? box)
        .focus({ preventScroll: true });
    }
    placeMenu();
  }

  function stateLine(text: string, retry?: () => void): HTMLElement {
    const p = el('p', 'state', text);
    if (retry != null) {
      const again = button('link', 'Zkusit znovu');
      again.dataset.key = 'retry';
      again.onclick = retry;
      p.append(' ', again);
    }
    return p;
  }

  /* The app's <PipelineStageMenu>: every stage as a row (the current one
   * checked), then removal behind its two-step confirm. */
  function buildStageMenu(box: HTMLElement, l: PortalListing, open: Menu): void {
    const propertyId = open.propertyId;
    const pending = busy.has(`p:${propertyId}`);
    box.setAttribute('role', 'menu');
    box.setAttribute('aria-label', STAGE_MENU_LABEL);
    box.appendChild(el('p', 'eyebrow', STAGE_MENU_LABEL));

    if (stages == null) {
      box.appendChild(stagesFailed
        ? stateLine('Fáze se nepodařilo načíst.', () => { void loadStages(); })
        : stateLine('Načítám fáze…'));
    } else {
      const list = el('div', 'list');
      for (const stage of stages) {
        const isCurrent = stage.id === l.pipeline?.stage_id;
        const accent = stageAccent(stage.color);
        const row = button('row');
        row.setAttribute('role', 'menuitemradio');
        row.setAttribute('aria-checked', String(isCurrent));
        row.dataset.key = `stage-${stage.id}`;
        setBusy(row, pending);
        const code = el('span', 'stage-code', stageBadge(stage.code, stage.id, stages) ?? '·');
        code.setAttribute('aria-hidden', 'true');
        code.style.color = accent.fg;
        row.append(code, el('span', 'name', stage.label));
        if (isCurrent) {
          const mark = el('span', 'mark', '✓');
          mark.setAttribute('aria-hidden', 'true');
          mark.style.color = accent.fg;
          row.appendChild(mark);
        }
        row.onclick = () => {
          if (busy.has(`p:${propertyId}`)) return;
          // Close first: the write repaints, and focus must already be back on the funnel.
          closeMenu(true);
          if (!isCurrent) void moveStage(propertyId, stage.id);
        };
        list.appendChild(row);
      }
      box.appendChild(list);
    }

    box.appendChild(el('div', 'sep'));
    if (!open.confirming) {
      const remove = button('row row--remove', 'Odebrat z pipeline');
      remove.setAttribute('role', 'menuitem');
      remove.dataset.key = 'remove';
      setBusy(remove, pending);
      remove.onclick = () => {
        if (busy.has(`p:${propertyId}`)) return;
        open.confirming = true;
        renderMenu();
        focusInMenu('.yes');
      };
      box.appendChild(remove);
      return;
    }

    /* Removal is the one pipeline action with no undo — the card's dates are
     * deleted and re-adding restamps them — so it asks, in the app's words,
     * and points at the closed stages, which keep the deal's record. */
    const confirm = el('div', 'confirm');
    confirm.appendChild(el('p', 'confirm-q', 'Odebrat z pipeline?'));
    confirm.appendChild(el(
      'p', 'confirm-d',
      'Karta zmizí z nástěnky a ztratí „v pipeline od“ i „ve fázi od“ — po '
        + 'opětovném přidání se počítají znovu.'
        + ((stages ?? []).some((s) => s.is_terminal)
          ? ' Uzavřený obchod raději přesuňte do některé z uzavřených fází.'
          : ''),
    ));
    const actions = el('div', 'confirm-actions');
    const yes = button('yes', 'Odebrat');
    yes.dataset.key = 'confirm-yes';
    yes.onclick = () => {
      closeMenu(true);
      void removeFromPipeline(propertyId);
    };
    const no = button('no', 'Zrušit');
    no.dataset.key = 'confirm-no';
    no.onclick = () => {
      open.confirming = false;
      renderMenu();
      focusInMenu('.row--remove');
    };
    actions.append(yes, no);
    confirm.appendChild(actions);
    box.appendChild(confirm);
  }

  /* The app's <CollectionSaveMenu>: every collection as a checkable row,
   * monitored ones first and bell-marked. It stays open across clicks — a
   * property can sit in several collections. */
  function buildCollectionMenu(box: HTMLElement, l: PortalListing, open: Menu): void {
    const propertyId = open.propertyId;
    const memberIds = new Set(l.collection_ids ?? []);
    const pending = busy.has(`c:${propertyId}`);
    box.setAttribute('role', 'group');
    box.setAttribute('aria-label', COLLECTION_SAVE_LABEL);
    box.appendChild(el('p', 'eyebrow', COLLECTION_SAVE_LABEL));

    if (collections == null) {
      /* A failed read is NOT an empty one — "no collections yet" would tell an
       * operator whose collections exist that they have none. */
      box.appendChild(collectionsFailed
        ? stateLine('Kolekce se nepodařilo načíst.', () => { void loadCollections(true); })
        : stateLine('Načítám kolekce…'));
    } else if (collections.length === 0) {
      box.appendChild(stateLine('Zatím nemáte žádnou kolekci.'));
    } else {
      const list = el('div', 'list');
      for (const c of sortCollections(collections)) {
        const member = memberIds.has(c.id);
        const row = button('row' + (member ? ' row--member' : ''));
        row.setAttribute('aria-pressed', String(member));
        row.dataset.key = `coll-${c.id}`;
        setBusy(row, pending);
        const check = el('span', 'check', '✓');
        check.setAttribute('aria-hidden', 'true');
        const name = el('span', 'name', c.name);
        name.title = c.name;
        row.append(check, name);
        if (c.monitoring_enabled) {
          const bell = el('span', 'bell');
          bell.title = 'Sledovaná — upozorní na změny';
          bell.innerHTML = bellIconSvg();
          row.appendChild(bell);
        }
        row.onclick = () => { void toggleCollection(propertyId, c.id); };
        list.appendChild(row);
      }
      box.appendChild(list);
    }

    if (open.error != null) {
      const err = el('p', 'error', open.error);
      err.setAttribute('role', 'alert');
      box.appendChild(err);
    }

    /* Creating, renaming and monitoring settings stay in the app — the
     * checklist only files the property. */
    if (deps.collectionsUrl != null) {
      const manage = el('a', 'link manage', collections?.length === 0
        ? 'Založit kolekci v aplikaci →'
        : 'Spravovat kolekce →');
      manage.href = deps.collectionsUrl;
      manage.target = '_blank';
      manage.rel = 'noopener';
      box.appendChild(manage);
    }
  }

  return { mount, clear, refresh, dismissedCount, reset };
}
