/* Portal registry — the extension's single source of truth for "which host
 * is which portal" and "how to pull a listing's native id out of a URL".
 *
 * Mirrors the backend's source set (the `portals` table / source_dispatcher).
 * The native id we extract here is exactly `listings.source_id_native`, so the
 * /listings/lookup endpoint resolves it directly. Sale-apartment gating and the
 * MF rent/yield come from the lookup, not from this file.
 *
 * The id extractor doubles as a "is this a detail URL?" test (returns null for
 * index/search pages) AND as the index-overlay matcher: on a search page we scan
 * every <a href> and keep the ones whose href yields an id — robust to the
 * portals' card-container markup changing under us. */

export interface PortalRef {
  source: string;
  sourceId: string;
}

export interface Portal {
  source: string;
  /* Hostnames this portal serves listings on (exact match, case-insensitive). */
  hosts: string[];
  /* Native id from a detail URL or a card's detail href; null = not a detail
   * link (so also: not a detail page). Accepts absolute or relative hrefs. */
  detailId(pathname: string): string | null;
  /* Best-effort "is this a sale apartment?" from the URL alone, for the
   * not-in-our-DB case where we have no row to gate on. true / false / null
   * (unknown). Only portals that encode category in the path implement it. */
  saleApartmentHint?(pathname: string): boolean | null;
}

function firstMatch(re: RegExp, s: string): string | null {
  const m = re.exec(s);
  return m ? m[1] : null;
}

/* sreality / idnes encode category as /…/{prodej|pronajem}/{byt|dum|…}/… */
function pathCategoryHint(path: string): boolean | null {
  const sale = /\/(prodej)\//.test(path);
  const rent = /\/(pronajem|pronájem)\//.test(path);
  const byt = /\/byt[\/-]/.test(path) || /\/byty\//.test(path);
  const otherKind = /\/(dum|domy|pozemek|pozemky|chata|chaty|kancelar|komercni|garaz)\b/.test(path);
  if (rent) return false;
  if (otherKind && !byt) return false;
  if (sale && byt) return true;
  return null;
}

export const PORTALS: Portal[] = [
  {
    source: 'sreality',
    hosts: ['www.sreality.cz', 'sreality.cz'],
    // /detail/{type}/{main}/{slug}/{id} — id is the trailing all-digits segment.
    detailId: (p) => firstMatch(/\/detail\/[^?#]*?\/(\d{5,})(?:[/?#]|$)/, p),
    saleApartmentHint: pathCategoryHint,
  },
  {
    source: 'bazos',
    hosts: ['reality.bazos.cz'],
    // /inzerat/{id}/{slug}.php
    detailId: (p) => firstMatch(/\/inzerat\/(\d+)\//, p),
  },
  {
    source: 'bezrealitky',
    hosts: ['www.bezrealitky.cz', 'bezrealitky.cz', 'bezrealitky.com'],
    // /nemovitosti-byty-domy/{id}-{slug}
    detailId: (p) => firstMatch(/\/nemovitosti-byty-domy\/(\d+)/, p),
  },
  {
    source: 'idnes',
    hosts: ['reality.idnes.cz'],
    // /detail/{sale}/{cat}/{slug}/{24-hex objectid}/
    detailId: (p) => firstMatch(/\/detail\/[^?#]*?\/([0-9a-f]{24})(?:[/?#]|$)/, p),
    saleApartmentHint: pathCategoryHint,
  },
  {
    source: 'maxima',
    hosts: ['nemovitosti.maxima.cz'],
    // /nemovitosti/{id}/  — id is a short letter+digits token (e.g. b50089333)
    detailId: (p) => firstMatch(/\/nemovitosti\/([a-z]?\d{6,})(?:[/?#]|$)/, p),
  },
  {
    source: 'remax',
    hosts: ['www.remax-czech.cz', 'remax-czech.cz'],
    // /reality/detail/{id}/{slug}
    detailId: (p) => firstMatch(/\/reality\/detail\/(\d+)/, p),
  },
  {
    // UNVERIFIED extractor (no live rows yet) — refine once mmreality has data.
    // A wrong id just yields found:false, never a bad badge.
    source: 'mmreality',
    hosts: ['www.mmreality.cz', 'mmreality.cz'],
    detailId: (p) => firstMatch(/\/(?:detail|zakazka)\/[^?#]*?(\d{6,})(?:[/?#]|$)/, p),
  },
  {
    // /{sale}/{cat}/{disp}/{town}/{slug}-{id}.html — id is the trailing run of
    // digits before ".html" (mirrors the scraper's _ID_RE). Verified on live URLs.
    source: 'ceskereality',
    hosts: ['www.ceskereality.cz', 'ceskereality.cz'],
    detailId: (p) => firstMatch(/-(\d{6,})\.html\b/, p),
    saleApartmentHint: pathCategoryHint,
  },
  {
    // /detail/{obec}/{slug}-{id}.html — id is the trailing run of digits before
    // ".html" (mirrors the scraper's _ID_RE). The detail URL does NOT encode the
    // category, so no saleApartmentHint (the /listings/lookup row gates instead).
    source: 'realitymix',
    hosts: ['www.realitymix.cz', 'realitymix.cz'],
    detailId: (p) => firstMatch(/-(\d{6,})\.html\b/, p),
  },
];

const BY_HOST: Map<string, Portal> = (() => {
  const m = new Map<string, Portal>();
  for (const portal of PORTALS) {
    for (const h of portal.hosts) m.set(h.toLowerCase(), portal);
  }
  return m;
})();

export function portalForHost(host: string): Portal | null {
  return BY_HOST.get(host.toLowerCase()) ?? null;
}

function parse(url: string): URL | null {
  try { return new URL(url, 'https://x'); } catch { return null; }
}

export function portalForUrl(url: string): Portal | null {
  const u = parse(url);
  if (u == null) return null;
  // Relative hrefs resolve against the dummy origin; fall back to current host.
  const host = u.hostname === 'x' ? location.hostname : u.hostname;
  return portalForHost(host);
}

/* (source, native id) for a detail URL/href, or null if it isn't one. For
 * relative card hrefs, `host` pins the portal (cards always link same-host). */
export function detailRef(url: string, host: string = location.hostname): PortalRef | null {
  const u = parse(url);
  if (u == null) return null;
  const portal = portalForHost(u.hostname === 'x' ? host : u.hostname);
  if (portal == null) return null;
  const id = portal.detailId(u.pathname);
  return id == null ? null : { source: portal.source, sourceId: id };
}

export function isDetailPage(url: string): boolean {
  return detailRef(url) != null;
}
