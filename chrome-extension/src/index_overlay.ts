/* Index / search-page overlay. We don't depend on any portal's card markup:
 * we scan every <a href>, keep the ones whose href yields a listing id via the
 * portal registry, and badge the nearest card-ish ancestor. One batched lookup
 * per pass; a result cache makes it cheap + resilient to SPA re-renders.
 *
 * Per sale-apartment card a single badge, ALWAYS clickable → opens the full
 * yield panel (MF rent/yield + the editable comparables calculator + run/view
 * estimation). Badge label: "Výnos MF X %" when we have it, else "Odhad X %"
 * when an estimation already exists, else "Odhadnout výnos".
 *
 * Sale-apartment gating: by our row's category when found; for listings not yet
 * in our DB, by the portal's URL category hint (sreality/idnes encode it in the
 * path) so freshly-listed cards still get the estimate affordance. */

import { detailRef, portalForHost, type Portal, type PortalRef } from './portals';
import type { ApiMessage, ApiResult, PortalListing } from './types';

type Caller = <T>(m: ApiMessage) => Promise<ApiResult<T>>;
type OpenPanel = (
  ref: PortalRef, url: string, prefetched?: PortalListing | null,
) => Promise<void>;

/* Holds the listing id the card was badged FOR, not a boolean: SPA routers
 * recycle card DOM nodes between result sets, so a node can still carry the
 * badge of the listing it previously held. Storing the id lets a recycled node
 * be detected and re-badged instead of silently showing another listing's yield. */
const PROCESSED_ATTR = 'data-mf-processed';
const BADGE_CLASS = '__mf_badge';
const STYLE_ID = '__mf_badge_style__';
const SCAN_DEBOUNCE_MS = 400;
const MAX_LOOKUP_PER_PASS = 50;

interface Hit {
  ref: PortalRef;
  anchor: HTMLAnchorElement;
  href: string;
}

function fmtPct(n: number | null): string {
  return n == null
    ? '—'
    : `${n.toLocaleString('cs-CZ', { minimumFractionDigits: 1, maximumFractionDigits: 1 })} %`;
}

function fmtCzk(n: number | null): string {
  return n == null ? '—' : `${Math.round(n).toLocaleString('cs-CZ')} Kč`;
}

/* Returns a stop() to disconnect the observer + cancel any pending scan —
 * called when a route change (SPA soft-nav) moves the tab off an index page,
 * so repeated navigations don't stack duplicate observers scanning the DOM. */
export async function runIndexOverlay(
  call: Caller, openPanel: OpenPanel,
): Promise<() => void> {
  const noop = (): void => {};
  const portal = portalForHost(location.hostname);
  if (portal == null) return noop;
  injectStyle();

  const cache = new Map<string, PortalListing>();

  let timer: ReturnType<typeof setTimeout> | null = null;
  const schedule = (): void => {
    if (timer != null) clearTimeout(timer);
    timer = setTimeout(() => { timer = null; void pass(); }, SCAN_DEBOUNCE_MS);
  };

  async function pass(): Promise<void> {
    const hits = collectHits(portal!.source);
    if (hits.length === 0) return;

    const needLookup = [...new Set(
      hits.filter((h) => !cache.has(h.ref.sourceId)).map((h) => h.ref.sourceId),
    )].slice(0, MAX_LOOKUP_PER_PASS);

    if (needLookup.length > 0) {
      const res = await call<PortalListing[]>({
        type: 'lookup_listings',
        items: needLookup.map((id) => ({ source: portal!.source, source_id: id })),
      });
      if (res.ok) for (const l of res.data) cache.set(l.source_id, l);
    }

    for (const hit of hits) {
      const listing = cache.get(hit.ref.sourceId);
      if (listing != null) process(hit, listing, portal!, openPanel);
    }
  }

  const obs = new MutationObserver(schedule);
  obs.observe(document.body, { childList: true, subtree: true });
  void pass();

  return () => {
    obs.disconnect();
    if (timer != null) clearTimeout(timer);
  };
}

function collectHits(source: string): Hit[] {
  const hits: Hit[] = [];
  const anchors = document.querySelectorAll<HTMLAnchorElement>('a[href]');
  for (const anchor of Array.from(anchors)) {
    const ref = detailRef(anchor.href, location.hostname);
    if (ref == null || ref.source !== source) continue;
    // Skip only if this card is already badged for THIS listing (see PROCESSED_ATTR).
    if (anchor.closest(`[${PROCESSED_ATTR}]`)?.getAttribute(PROCESSED_ATTR) === ref.sourceId) {
      continue;
    }
    hits.push({ ref, anchor, href: anchor.href });
  }
  return hits;
}

function cardFor(anchor: HTMLAnchorElement): HTMLElement {
  const card = anchor.closest(
    'li, article, [class*="item"], [class*="card"], [class*="result"], [class*="estate"]',
  );
  return (card as HTMLElement | null) ?? anchor.parentElement ?? anchor;
}

/* sreality/idnes encode prodej/byt in the detail path; other portals return null. */
function urlSaleHint(portal: Portal, href: string): boolean | null {
  if (portal.saleApartmentHint == null) return null;
  try {
    return portal.saleApartmentHint(new URL(href).pathname);
  } catch {
    return null;
  }
}

function process(hit: Hit, listing: PortalListing, portal: Portal, openPanel: OpenPanel): void {
  const card = cardFor(hit.anchor);
  const prevId = card.getAttribute(PROCESSED_ATTR);
  if (prevId === hit.ref.sourceId) return;
  // Recycled node — drop the previous listing's badge before re-badging.
  if (prevId != null) card.querySelector(`:scope > .${BADGE_CLASS}`)?.remove();
  card.setAttribute(PROCESSED_ATTR, hit.ref.sourceId);

  const saleApt = listing.found
    ? listing.category_main === 'byt' && listing.category_type === 'prodej'
    : urlSaleHint(portal, hit.href) === true;
  if (!saleApt) return;

  if (getComputedStyle(card).position === 'static') card.style.position = 'relative';

  const badge = document.createElement('div');
  badge.className = BADGE_CLASS;
  badge.setAttribute('role', 'button');
  badge.title = 'Klikni pro odhad výnosu';

  if (listing.found && listing.mf_gross_yield_pct != null) {
    badge.classList.add('__mf_badge--yield');
    badge.textContent = `Výnos MF ${fmtPct(listing.mf_gross_yield_pct)}`;
    if (listing.mf_reference_rent_czk != null) {
      badge.title = `MF nájem ${fmtCzk(listing.mf_reference_rent_czk)}/měs · klikni pro odhad`;
    }
  } else if (listing.latest_estimation?.gross_yield_pct != null) {
    badge.classList.add('__mf_badge--est');
    badge.textContent = `Odhad ${fmtPct(listing.latest_estimation.gross_yield_pct)}`;
  } else {
    badge.classList.add('__mf_badge--cta');
    badge.textContent = 'Odhadnout výnos';
  }

  badge.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    void openPanel(hit.ref, hit.href, listing);
  });
  card.appendChild(badge);
}

function injectStyle(): void {
  if (document.getElementById(STYLE_ID) != null) return;
  const style = document.createElement('style');
  style.id = STYLE_ID;
  /* Scoped class + explicit properties — index badges live in the portal's
   * DOM (not a shadow root), so we spell out everything to resist CSS bleed. */
  style.textContent = `
    .__mf_badge {
      position: absolute; top: 6px; left: 6px; z-index: 2147483646;
      font-family: system-ui, -apple-system, sans-serif; font-size: 11px;
      font-weight: 600; line-height: 1; letter-spacing: 0.02em;
      padding: 4px 7px; border: 1px solid #1c1c1c; border-radius: 0;
      font-variant-numeric: tabular-nums; white-space: nowrap; cursor: pointer;
      box-shadow: 0 1px 3px rgba(0,0,0,0.12); pointer-events: auto;
    }
    .__mf_badge--yield { background: #b3592d; color: #fff; }
    .__mf_badge--est { background: #555; color: #fff; }
    .__mf_badge--cta { background: #f7f3ec; color: #b3592d; }
    .__mf_badge--cta:hover { background: #f3eadf; }
    .__mf_badge--yield:hover, .__mf_badge--est:hover { filter: brightness(1.08); }
  `;
  (document.head ?? document.documentElement).appendChild(style);
}
