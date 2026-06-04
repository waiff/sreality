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

const PROCESSED_ATTR = 'data-mf-processed';
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

export async function runIndexOverlay(call: Caller, openPanel: OpenPanel): Promise<void> {
  const portal = portalForHost(location.hostname);
  if (portal == null) return;
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
}

function collectHits(source: string): Hit[] {
  const hits: Hit[] = [];
  const anchors = document.querySelectorAll<HTMLAnchorElement>('a[href]');
  for (const anchor of Array.from(anchors)) {
    if (anchor.closest(`[${PROCESSED_ATTR}]`) != null) continue;
    const ref = detailRef(anchor.href, location.hostname);
    if (ref == null || ref.source !== source) continue;
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
  if (card.getAttribute(PROCESSED_ATTR) != null) return;
  card.setAttribute(PROCESSED_ATTR, '1');

  const saleApt = listing.found
    ? listing.category_main === 'byt' && listing.category_type === 'prodej'
    : urlSaleHint(portal, hit.href) === true;
  if (!saleApt) return;

  if (getComputedStyle(card).position === 'static') card.style.position = 'relative';

  const badge = document.createElement('div');
  badge.className = '__mf_badge';
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
