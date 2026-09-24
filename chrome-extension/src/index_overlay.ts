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
 * path) so freshly-listed cards still get the estimate affordance.
 *
 * Badges only ever come from a SUCCESSFUL lookup. When it fails (signed out,
 * network / API error) the page gets one corner notice instead — see
 * "the failure notice" below. */

// The shared product brand (frontend/src/lib/brand.ts) — the notice's wordmark
// is the same string as the panel header's.
import { APP_NAME } from '../../frontend/src/lib/brand';
import { detailRef, portalForHost, type Portal, type PortalRef } from './portals';
import type { ApiMessage, ApiResult, PortalListing } from './types';

type Caller = <T>(m: ApiMessage) => Promise<ApiResult<T>>;
type OpenPanel = (
  ref: PortalRef, url: string, prefetched?: PortalListing | null,
) => Promise<void>;

/* Holds the listing id the card was badged FOR, not a boolean: SPA routers
 * recycle card DOM nodes between result sets, so a node can still carry the
 * badge of the listing it previously held. Storing the id lets a recycled node
 * be detected and re-badged (or, while its lookup is failing, unbadged) instead
 * of silently showing another listing's yield. */
const PROCESSED_ATTR = 'data-mf-processed';
const BADGE_CLASS = '__mf_badge';
const STYLE_ID = '__mf_badge_style__';
const SCAN_DEBOUNCE_MS = 400;
const MAX_LOOKUP_PER_PASS = 50;

/* ---- the failure notice --------------------------------------------------
 *
 * A failed lookup leaves nothing to badge, and the page used to stay silent: a
 * signed-out operator got no badge, no CTA and no sign-in prompt — while a
 * detail page in the same state shows the panel's prompt — and read it as a
 * broken extension. Now a failure raises ONE page-level notice in the panel's
 * corner, offering what can fix it: sign-in when signed out, a page reload when
 * an extension update orphaned this script, a retry for other errors, nothing
 * for a build without an API URL. Page-level, not per-card: for listings we
 * have no row for, the sale-apartment gate is a URL hint only
 * sreality/idnes/ceskereality provide, so a per-card prompt would be invisible
 * on the other portals. It shows only while cards the URL doesn't rule out as
 * sale apartments wait on the failed lookup — never on a page without listing
 * cards. On those three portals that also keeps it off rental and house
 * searches; elsewhere any card counts. It steps aside while the panel is open
 * (a badge click opens it in the same corner, with its own sign-in prompt).
 * × holds for the overlay's lifetime.
 *
 * Backoff: after a failure, observer-driven passes don't ask again for
 * LOOKUP_RETRY_AFTER_MS — a 401 costs no network, but in a real outage every
 * DOM mutation (re-renders, infinite scroll, the notice mounting itself) would
 * re-fetch. The notice's button skips the wait.
 *
 * Generations: one lookup is out at a time, but a sign-in, a sign-out or the
 * retry button starts a new generation. Its lookup goes out even while an
 * older one is pending (a hung request can't swallow the click), and answers
 * to older asks are dropped (an old 401 landing after a sign-in would raise a
 * false notice; an old session's rows would badge the new one's cards). A
 * lookup unanswered after LOOKUP_TIMEOUT_MS counts as failed, so a hung one
 * can't leave the page silent either.
 *
 * Sessions: a sign-in or sign-out anywhere — this notice, the panel, another
 * tab — rewrites the session in chrome.storage, so the overlay watches that
 * key. The failure clears and the cards get their lookup without a second
 * click here. Rows are account-scoped (estimation, pipeline, collections), so
 * a session change also drops the cached rows and the badges built from them.
 *
 * Teardown: stop() removes the notice, and everything that awaits re-checks
 * `stopped` — a sign-in or lookup resolving after a route change must not
 * re-mount the notice or re-scan a page this overlay no longer owns. */
const NOTICE_HOST_ID = '__mf_notice_host__';
const LOOKUP_RETRY_AFTER_MS = 60_000;
/* Prod answers in well under a second; past this a lookup counts as failed. */
const LOOKUP_TIMEOUT_MS = 20_000;
/* A live region inserted already filled is typically not announced, so the
 * notice goes in empty and gets its text a frame + this long later. */
const LIVE_REGION_SETTLE_MS = 100;

/* content.ts's HOST_ELEMENT_ID and auth.ts's SESSION_KEY, by value: content.ts
 * imports this module (not the reverse), and auth.ts runs only in the
 * background service worker. */
const PANEL_HOST_ID = '__sreality_yield_panel_host__';
const SESSION_KEY = 'authSession';

/* Mirrors api.ts's NOT_SIGNED_IN_DETAIL — duplicated by value, not imported, as
 * content.ts does: api.ts runs only in the background service worker, and the
 * content bundle never fetches directly. */
const NOT_SIGNED_IN_DETAIL = 'not_signed_in';
/* api.ts's API_NOT_CONFIGURED_DETAIL, by value for the same reason: a build
 * without VITE_API_BASE_URL, which neither a retry nor a reload can fix. */
const API_NOT_CONFIGURED_DETAIL = 'API base URL not configured';

/* call()'s detail when an extension reload or update has orphaned this content
 * script (content.ts's runtimeDetail uses it; it lives here because content.ts
 * imports this module, not the reverse). Only a page reload helps. */
export const EXTENSION_RELOADED_DETAIL = 'Rozšíření bylo aktualizováno — obnovte stránku';

interface Hit {
  ref: PortalRef;
  anchor: HTMLAnchorElement;
  href: string;
}

/* What the notice offers: sign in; reload the page; retry; or nothing. */
type FailureKind = 'signed_out' | 'reload' | 'error' | 'fatal';

interface LookupFailure {
  kind: FailureKind;
  detail: string;
}

interface NoticeView {
  failure: LookupFailure;
  signingIn: boolean;
  retrying: boolean;
  signInError: string | null;
}

interface NoticeHandlers {
  signIn: () => void;
  retry: () => void;
  reload: () => void;
  dismiss: () => void;
}

interface NoticeHandle {
  render: (view: NoticeView) => void;
  destroy: () => void;
}

function fmtPct(n: number | null): string {
  return n == null
    ? '—'
    : `${n.toLocaleString('cs-CZ', { minimumFractionDigits: 1, maximumFractionDigits: 1 })} %`;
}

function fmtCzk(n: number | null): string {
  return n == null ? '—' : `${Math.round(n).toLocaleString('cs-CZ')} Kč`;
}

function failureKind(detail: string): FailureKind {
  if (detail === NOT_SIGNED_IN_DETAIL) return 'signed_out';
  if (detail === EXTENSION_RELOADED_DETAIL) return 'reload';
  if (detail === API_NOT_CONFIGURED_DETAIL) return 'fatal';
  return 'error';
}

/* call() with a deadline: a lookup the background never answers (a wedged
 * API, a stuck worker) turns into a failure the notice can show and retry. */
function withTimeout<T>(pending: Promise<ApiResult<T>>, ms: number): Promise<ApiResult<T>> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const late = new Promise<ApiResult<T>>((resolve) => {
    timer = setTimeout(() => resolve({
      ok: false, status: 0, detail: `Server neodpověděl do ${ms / 1000} s`,
    }), ms);
  });
  return Promise.race([pending, late]).finally(() => clearTimeout(timer));
}

/* Returns a stop() to disconnect the observer + cancel any pending scan + remove
 * the failure notice — called when a route change (SPA soft-nav) moves the tab
 * off an index page, so repeated navigations don't stack duplicate observers
 * scanning the DOM. */
export async function runIndexOverlay(
  call: Caller, openPanel: OpenPanel,
): Promise<() => void> {
  const noop = (): void => {};
  const portal = portalForHost(location.hostname);
  if (portal == null) return noop;
  injectStyle();

  const cache = new Map<string, PortalListing>();

  let stopped = false;
  let failure: LookupFailure | null = null;
  let failedAt: number | null = null;  // backoff anchor (monotonic); null = free to look up
  let cardsWaiting = false;  // the last pass saw possible sale cards with no successful lookup
  let dismissed = false;
  let signingIn = false;
  let retrying = false;
  let signInError: string | null = null;
  let notice: NoticeHandle | null = null;
  let gen = 0;  // bumped by a sign-in, a sign-out or a retry: older answers are stale
  let inFlightGen: number | null = null;  // generation of the lookup that is out
  let rescan = false;  // a pass skipped its lookup because this generation's was out
  let sessionKnown = false;  // a sign-in or a successful lookup since the last 401 / sign-out

  let timer: ReturnType<typeof setTimeout> | null = null;
  const schedule = (): void => {
    if (timer != null) clearTimeout(timer);
    timer = setTimeout(() => { timer = null; void pass(); }, SCAN_DEBOUNCE_MS);
  };

  // performance.now, not Date.now: a wall-clock step back must not stretch it.
  const backingOff = (): boolean =>
    failedAt != null && performance.now() - failedAt < LOOKUP_RETRY_AFTER_MS;

  /* The notice is a pure function of the state above: shown, updated in place,
   * or removed — never a second host. */
  function syncNotice(): void {
    const panelOpen = document.getElementById(PANEL_HOST_ID) != null;
    if (stopped || dismissed || failure == null || !cardsWaiting || panelOpen) {
      notice?.destroy();
      notice = null;
      return;
    }
    notice ??= mountNotice({
      signIn: () => { void signIn(); },
      retry: () => { void retry(); },
      reload: () => { location.reload(); },
      dismiss: () => { dismissed = true; syncNotice(); },
    });
    notice.render({ failure, signingIn, retrying, signInError });
  }

  /* A new session, or none: start a new generation and forget the old
   * session's failure, rows and badges. */
  function sessionChanged(): void {
    gen++;
    failure = null;
    failedAt = null;
    signInError = null;
    cache.clear();
    for (const card of Array.from(document.querySelectorAll<HTMLElement>(`[${PROCESSED_ATTR}]`))) {
      clearBadge(card);
    }
    syncNotice();
  }

  /* False for a session this overlay already knows: the storage broadcast of
   * the notice's own sign-in can land after its answer, and must not drop the
   * lookup that answer already sent. */
  function sessionAppeared(): boolean {
    if (sessionKnown) return false;
    sessionKnown = true;
    sessionChanged();
    return true;
  }

  async function lookup(hits: Hit[]): Promise<void> {
    const ids = [...new Set(
      hits.filter((h) => !cache.has(h.ref.sourceId)).map((h) => h.ref.sourceId),
    )].slice(0, MAX_LOOKUP_PER_PASS);
    if (ids.length === 0 || backingOff()) return;
    if (inFlightGen === gen) {
      rescan = true;
      return;
    }
    const asked = gen;
    inFlightGen = asked;
    let res: ApiResult<PortalListing[]>;
    try {
      res = await withTimeout(call<PortalListing[]>({
        type: 'lookup_listings',
        items: ids.map((id) => ({ source: portal!.source, source_id: id })),
      }), LOOKUP_TIMEOUT_MS);
    } finally {
      if (inFlightGen === asked) inFlightGen = null;
    }
    // Superseded: the newer generation's pass asks (or already has) for itself.
    if (stopped || asked !== gen) return;
    if (res.ok) {
      for (const l of res.data) cache.set(l.source_id, l);
      sessionKnown = true;
      failure = null;
      failedAt = null;
      signInError = null;
    } else {
      const kind = failureKind(res.detail);
      if (kind === 'signed_out') sessionKnown = false;
      if (failure?.kind !== kind) signInError = null;
      failure = { kind, detail: res.detail };
      failedAt = performance.now();
    }
    if (rescan) {
      rescan = false;
      schedule();
    }
  }

  async function pass(): Promise<void> {
    if (stopped) return;
    let hits = collectHits(portal!.source);
    if (hits.length > 0) {
      await lookup(hits);
      if (stopped) return;
      /* The page can change while the lookup is out (a soft-nav to a page
       * without cards, a filter swapping sale cards for rentals): the notice
       * and the badges act on the page as it is now. */
      hits = collectHits(portal!.source);
    }

    cardsWaiting = hits.some(
      (h) => !cache.has(h.ref.sourceId) && urlSaleHint(portal!, h.href) !== false,
    );
    syncNotice();

    for (const hit of hits) {
      const listing = cache.get(hit.ref.sourceId);
      if (listing != null) process(hit, listing, portal!, openPanel);
      else unbadgeRecycled(hit);
    }
  }

  /* The same background-owned PKCE sign-in the panel's prompt runs (a content
   * script can't reach chrome.identity); on success the cards get their lookup. */
  async function signIn(): Promise<void> {
    if (signingIn) return;
    signingIn = true;
    signInError = null;
    syncNotice();
    let ok = false;
    try {
      const res = await call<undefined>({ type: 'sign_in' });
      ok = res.ok;
      if (!res.ok) signInError = `Přihlášení selhalo: ${res.detail}`;
    } finally {
      signingIn = false;
      syncNotice();
    }
    if (!ok || stopped) return;
    sessionAppeared();  // a no-op when the storage broadcast brought it first
    await pass();
  }

  /* Asks now, past the backoff and past a lookup that is still out; the
   * button reads "Načítám…" until this lookup settles. */
  async function retry(): Promise<void> {
    if (retrying) return;
    retrying = true;
    failedAt = null;
    gen++;
    syncNotice();
    try {
      await pass();
    } finally {
      retrying = false;
      syncNotice();
    }
  }

  /* A sign-out, or a sign-in: a session where there was none, or a rewrite
   * while this page is signed out. Any other rewrite is a token refresh. */
  const onStorage = (
    changes: { [key: string]: chrome.storage.StorageChange }, area: string,
  ): void => {
    const change = changes[SESSION_KEY];
    if (stopped || area !== 'local' || change == null) return;
    if (change.newValue == null) {
      if (change.oldValue == null) return;
      sessionKnown = false;
      sessionChanged();
      schedule();
      return;
    }
    if (change.oldValue != null && failure?.kind !== 'signed_out') return;
    if (sessionAppeared()) schedule();
  };

  const obs = new MutationObserver(schedule);
  obs.observe(document.body, { childList: true, subtree: true });
  /* The panel host sits on <html>, outside the body observer's reach; the
   * notice steps aside while it's open and comes back when it closes. */
  const panelObs = new MutationObserver(() => { syncNotice(); });
  panelObs.observe(document.documentElement, { childList: true });
  chrome.storage.onChanged?.addListener(onStorage);
  void pass();

  return () => {
    stopped = true;
    obs.disconnect();
    panelObs.disconnect();
    chrome.storage.onChanged?.removeListener(onStorage);
    if (timer != null) clearTimeout(timer);
    syncNotice();
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

function clearBadge(card: HTMLElement): void {
  card.querySelector(`:scope > .${BADGE_CLASS}`)?.remove();
  card.removeAttribute(PROCESSED_ATTR);
}

/* A card still waiting on its lookup whose node was recycled from another
 * listing (see PROCESSED_ATTR) drops that listing's badge and click target now,
 * not on the next successful lookup — a failing or backed-off lookup would
 * leave it up for a whole backoff window. A card that still links the badged
 * listing wasn't recycled, just holds two links; it is left alone. */
function unbadgeRecycled(hit: Hit): void {
  const card = cardFor(hit.anchor);
  const prevId = card.getAttribute(PROCESSED_ATTR);
  if (prevId == null || prevId === hit.ref.sourceId) return;
  const stillLinked = Array.from(card.querySelectorAll<HTMLAnchorElement>('a[href]'))
    .some((a) => detailRef(a.href, location.hostname)?.sourceId === prevId);
  if (!stillLinked) clearBadge(card);
}

function process(hit: Hit, listing: PortalListing, portal: Portal, openPanel: OpenPanel): void {
  const card = cardFor(hit.anchor);
  const prevId = card.getAttribute(PROCESSED_ATTR);
  if (prevId === hit.ref.sourceId) return;
  // Recycled node — drop the previous listing's badge before re-badging.
  if (prevId != null) clearBadge(card);
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

/* The notice's own stylesheet — a closed shadow root like the panel's, so the
 * portal's CSS can't reach in. The panel's civic-archive palette by value:
 * paper surface, ink edge, the 2px copper "filed" top edge, a copper button. */
const NOTICE_CSS = `
  :host { all: initial; }
  [hidden] { display: none !important; }
  .__mf_notice {
    position: fixed; right: 1.25rem; bottom: 1.25rem; z-index: 2147483646;
    box-sizing: border-box; width: max-content;
    max-width: min(21rem, calc(100vw - 2.5rem));
    padding: 0.6rem 0.85rem 0.8rem;
    font-family: 'Inter', system-ui, -apple-system, sans-serif;
    font-size: 0.82rem; line-height: 1.45; text-align: left; color: #1c1c1c;
    background: #f7f3ec; border: 1px solid #1c1c1c;
    box-shadow: inset 0 2px 0 #b3592d, 0 10px 34px -10px rgba(28, 20, 10, 0.30);
    transition: opacity 140ms ease;
  }
  /* Transparent, not hidden: the empty live region must already be in the
   * accessibility tree when its first text arrives. */
  .__mf_notice.is-pending { opacity: 0; pointer-events: none; }
  @media (prefers-reduced-motion: reduce) { .__mf_notice { transition: none; } }
  .n-head {
    display: flex; align-items: center; justify-content: space-between;
    gap: 1rem; margin-bottom: 0.35rem;
  }
  .n-mark { display: inline-flex; align-items: center; gap: 0.45rem; }
  .n-tick { width: 0.5rem; height: 0.5rem; background: #b3592d; flex-shrink: 0; }
  .n-word {
    font-size: 0.64rem; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.2em; color: #b3592d;
  }
  .n-close {
    margin: -0.2rem -0.3rem -0.2rem 0; padding: 0 0.2rem;
    font: inherit; font-size: 1.1rem; line-height: 1; color: #8a8a8a;
    background: transparent; border: 0; cursor: pointer;
  }
  .n-close:hover { color: #1c1c1c; }
  .n-text { margin: 0; }
  .n-detail, .n-error {
    margin: 0.15rem 0 0; font-size: 0.74rem; overflow-wrap: anywhere;
  }
  .n-detail { color: #555; }
  .n-error { color: #b34730; }
  .n-primary {
    display: block; width: 100%; margin-top: 0.6rem; padding: 0.5rem 0.9rem;
    font: inherit; font-size: 0.72rem; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.1em; color: #fff; background: #b3592d; border: 0;
    cursor: pointer; transition: background 120ms ease;
  }
  .n-primary:hover { background: #9a4b25; }
  .n-primary[aria-disabled="true"] { background: #8a8a8a; cursor: wait; }
  .n-close:focus-visible, .n-primary:focus-visible {
    outline: 2px solid #b3592d; outline-offset: 2px;
  }
`;

/* Built once, then updated in place: the live region keeps its node (so screen
 * readers announce changes, not a remount) and a focused button keeps focus —
 * busy is aria-disabled, because `disabled` would drop focus to <body>, behind
 * every focusable element of the portal page. The first text waits
 * LIVE_REGION_SETTLE_MS after mount (see there). */
function mountNotice(on: NoticeHandlers): NoticeHandle {
  document.getElementById(NOTICE_HOST_ID)?.remove();
  const host = document.createElement('div');
  host.id = NOTICE_HOST_ID;
  const shadow = host.attachShadow({ mode: 'closed' });
  const style = document.createElement('style');
  style.textContent = NOTICE_CSS;
  shadow.appendChild(style);

  const box = el('div', '__mf_notice is-pending');
  const head = el('div', 'n-head');
  const mark = el('span', 'n-mark');
  mark.append(el('span', 'n-tick'), el('span', 'n-word', APP_NAME));
  const close = button('n-close', '×');
  close.title = 'Skrýt';
  close.setAttribute('aria-label', 'Skrýt');
  close.onclick = on.dismiss;
  head.append(mark, close);

  const body = el('div', 'n-body');
  body.setAttribute('role', 'status');
  body.setAttribute('aria-live', 'polite');
  const text = el('p', 'n-text');
  const detail = el('p', 'n-detail');
  const error = el('p', 'n-error');
  body.append(text, detail, error);

  let live = false;  // the first text is in (the box is transparent until then)
  let kind: FailureKind = 'error';
  let busy = false;
  const action = button('n-primary', '');
  action.onclick = () => {
    if (!live || busy) return;
    if (kind === 'signed_out') on.signIn();
    else if (kind === 'reload') on.reload();
    else if (kind === 'error') on.retry();
  };

  box.append(head, body, action);
  shadow.appendChild(box);
  document.body.appendChild(host);

  let latest: NoticeView | null = null;
  let settle: ReturnType<typeof setTimeout> | null = null;
  const frame = requestAnimationFrame(() => {
    settle = setTimeout(() => {
      live = true;
      box.classList.remove('is-pending');
      if (latest != null) paint(latest);
    }, LIVE_REGION_SETTLE_MS);
  });

  function paint(view: NoticeView): void {
    kind = view.failure.kind;
    const signedOut = kind === 'signed_out';
    busy = signedOut ? view.signingIn : kind === 'error' && view.retrying;
    const failLine = signedOut ? view.signInError : null;
    setText(text, signedOut
      ? 'Pro výnosy na kartách se prosím přihlaste.'
      : 'Výnosy se nepodařilo načíst.');
    setText(detail, signedOut ? '' : view.failure.detail);
    detail.hidden = signedOut;
    setText(error, failLine ?? '');
    error.hidden = failLine == null;
    if (busy) action.setAttribute('aria-disabled', 'true');
    else action.removeAttribute('aria-disabled');
    action.hidden = kind === 'fatal';
    setText(action, {
      signed_out: busy ? 'Přihlašuji…' : 'Přihlásit se přes Google',
      reload: 'Obnovit stránku',
      error: busy ? 'Načítám…' : 'Zkusit znovu',
      fatal: '',
    }[kind]);
  }

  return {
    render(view: NoticeView): void {
      latest = view;
      if (live) paint(view);
    },
    destroy(): void {
      cancelAnimationFrame(frame);
      if (settle != null) clearTimeout(settle);
      host.remove();
    },
  };
}

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K, className: string, text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function button(className: string, text: string): HTMLButtonElement {
  const b = el('button', className, text);
  b.type = 'button';
  return b;
}

/* Only on change — rewriting a live region's text re-announces it, and passes
 * re-render the notice on every DOM mutation while it is up. */
function setText(node: HTMLElement, value: string): void {
  if (node.textContent !== value) node.textContent = value;
}
