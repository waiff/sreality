/* Listing-page content script for every portal we scrape.
 *
 * Detail pages → a floating panel (closed shadow root) that shows our
 * precomputed "Výnos MF" gross yield + MF reference rent for sale apartments,
 * with the comparables estimation as the deeper tool / fallback. The panel is
 * visibly deactivated for anything that isn't an apartment for sale.
 *
 * Index/search pages → small per-card badges (see index_overlay.ts).
 *
 * All network calls go through chrome.runtime.sendMessage to the background
 * worker (host_permissions + the portal's CORS don't apply there). */

import styles from './styles.css?inline';
import { detailRef, portalForHost, portalForUrl, type PortalRef } from './portals';
import { runIndexOverlay } from './index_overlay';
import type {
  ApiMessage,
  ApiResult,
  AuthState,
  CollectionWriteResult,
  EstimationRun,
  ExtCollection,
  ExtNote,
  PipelineCardResult,
  PipelineStage,
  PortalListing,
  YieldScenarioUpdate,
} from './types';
// Shared product brand — the ONE definition (frontend/src/lib/brand.ts), the
// same one the SPA uses. Rebranding there updates the panel wordmark here too.
import { APP_NAME } from '../../frontend/src/lib/brand';

const DEFAULT_FOND_CZK_PER_M2 = 10;
const DEFAULT_RENOVATION_CZK = 0;
const PATCH_DEBOUNCE_MS = 500;
const POLL_INTERVAL_MS = 2000;
const POLL_MAX_ATTEMPTS = 60;
const HOST_ELEMENT_ID = '__sreality_yield_panel_host__';

/* Minimized = the panel collapses to a tiny bar showing just the two yield
 * figures. The preference persists across listings/pages via chrome.storage.local
 * (the "storage" permission), so once the operator tucks it away it stays small
 * while they browse. Loaded once on boot; openPanel awaits it before first paint
 * so there's no expand→collapse flash. */
const MINIMIZED_KEY = 'panelMinimized';
let minimized = false;
const minimizedReady: Promise<void> = chrome.storage.local
  .get([MINIMIZED_KEY])
  .then((r) => { minimized = r[MINIMIZED_KEY] === true; })
  .catch(() => { /* storage unavailable → default to expanded */ });

/* SPA base URL for the "Otevřít v aplikaci" deep-link, inlined at build time.
 * Inlined here (not shared with api.ts) because MV3 content scripts are classic
 * scripts that can't `import` — content.js must stay self-contained. Empty →
 * link hidden. Default https when the operator omits the scheme. */
const APP_BASE_URL = ((raw: string): string => {
  const t = raw.trim();
  if (t === '') return '';
  return (/^https?:\/\//i.test(t) ? t : `https://${t}`).replace(/\/$/, '');
})(import.meta.env.VITE_APP_BASE_URL ?? '');

type Phase = 'loading' | 'deactivated' | 'active' | 'error' | 'signed_out';

/* Mirrors api.ts's NOT_SIGNED_IN_DETAIL — duplicated, not imported, for the
 * same reason normalizeBaseUrl is duplicated above: api.ts/auth.ts run only
 * in the background service worker (chrome.identity, direct fetches), and
 * content.ts must stay a self-contained bundle that never fetches directly. */
const NOT_SIGNED_IN_DETAIL = 'not_signed_in';

function friendlyDetail(detail: string): string {
  return detail === NOT_SIGNED_IN_DETAIL ? 'nejste přihlášeni' : detail;
}

interface PanelState {
  phase: Phase;
  /* The signed-in operator's email (Wave 1 extension login), or null when
   * signed out / not yet loaded. Drives the sign-out control in the header. */
  authEmail: string | null;
  /* Our scraped facts + MF rent/yield for the subject listing. */
  listing: PortalListing | null;
  /* byt+prodej? Gates the MF + estimation blocks; the app link + facts show
   * regardless. null = unknown (not in our DB and no URL category hint). */
  isSaleApt: boolean | null;
  /* Optional comparables estimation (full row) for the editable yield block. */
  run: EstimationRun | null;
  /* Per-axis "touched" — null + touched=false means "follow the default". */
  rentTouched: boolean;
  costTouched: boolean;
  priceTouched: boolean;
  renovationTouched: boolean;
  rent: number | null;
  costPerM2: number | null;
  price: number | null;
  /* Flat one-off renovation budget, added to price for the yield denominator. */
  renovation: number | null;
  /* True while an estimation is being created/polled. */
  busy: boolean;
  /* True while a pipeline add/remove/move is in flight (disables the control). */
  pipelineBusy: boolean;
  /* Operator-curated stage list for the stage `<select>` (null = not loaded). */
  stages: PipelineStage[] | null;
  /* Operator-curated collection list for the monitoring toggle (null = not loaded). */
  collections: ExtCollection[] | null;
  /* True while a collection add/remove is in flight (disables the control). */
  collectionBusy: boolean;
  /* The property's operator notes (null = not loaded). Per-property, not cached. */
  notes: ExtNote[] | null;
  /* True while a note save is in flight (disables the add box). */
  noteBusy: boolean;
  errorMessage: string | null;
}

export function call<T>(message: ApiMessage): Promise<ApiResult<T>> {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(message, (response: ApiResult<T>) => {
      if (chrome.runtime.lastError) {
        resolve({
          ok: false,
          status: 0,
          detail: chrome.runtime.lastError.message ?? 'runtime error',
        });
        return;
      }
      resolve(response);
    });
  });
}

// ----------------------------------------------------------------------
// Formatting
// ----------------------------------------------------------------------

function fmtCzk(n: number | null | undefined): string {
  return n == null ? '—' : `${Math.round(n).toLocaleString('cs-CZ')} Kč`;
}

/* A note's timestamp as a compact cs-CZ date (full datetime in the title attr). */
function fmtNoteDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleDateString('cs-CZ', { day: 'numeric', month: 'numeric', year: 'numeric' });
}

function fmtPct(n: number | null | undefined): string {
  return n == null
    ? '—'
    : `${n.toLocaleString('cs-CZ', { minimumFractionDigits: 1, maximumFractionDigits: 1 })} %`;
}

function formatNumber(n: number | null): string {
  if (n == null || !Number.isFinite(n)) return '';
  return String(n);
}

function parseNumber(raw: string): number | null {
  const trimmed = raw.trim().replace(/\s/g, '').replace(',', '.');
  if (trimmed === '') return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : null;
}

/* The SPA's <FunnelIcon> (icons.tsx) hand-reproduced as inline SVG — the shared
 * "pipeline" glyph on every surface (a funnel with three arrows; filled body =
 * in-pipeline). The extension can't import the SPA's React component (separate
 * territory, classic content script), so this mirrors it by value, like the
 * palette in styles.css. */
function funnelIconSvg(filled: boolean): string {
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

/* Immutably set the listing's pipeline membership on a state update. */
function withPipeline(
  prev: PanelState, membership: PortalListing['pipeline'],
): PanelState {
  if (prev.listing == null) return prev;
  return { ...prev, listing: { ...prev.listing, pipeline: membership } };
}

/* Immutably set the listing's collection memberships on a state update. */
function withCollections(
  prev: PanelState, collection_ids: PortalListing['collection_ids'],
): PanelState {
  if (prev.listing == null) return prev;
  return { ...prev, listing: { ...prev.listing, collection_ids } };
}

/* Pick the single collection the one-click monitoring toggle targets: the
 * system "monitoring" collection if present (`is_system`), else the first
 * monitoring-enabled collection. null = the operator hasn't set monitoring up,
 * so the toggle renders nothing. */
function monitoringTarget(collections: ExtCollection[] | null): ExtCollection | null {
  if (collections == null) return null;
  return (
    collections.find((c) => c.is_system)
    ?? collections.find((c) => c.monitoring_enabled)
    ?? null
  );
}

/* A bell glyph for the monitoring affordance — DISTINCT from the funnel so the
 * two adjacent controls read differently (the funnel = deal pipeline, the bell =
 * watch / monitoring). Filled body = being monitored. Hand-coded inline SVG (no
 * React import — separate territory), like funnelIconSvg above. */
function bellIconSvg(filled: boolean): string {
  const f = filled ? 'currentColor' : 'none';
  return (
    '<svg class="collection-icon" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="1.75" stroke-linecap="round" ' +
    'stroke-linejoin="round" aria-hidden="true">' +
    `<path d="M6 9 a6 6 0 0 1 12 0 c0 5 1.5 6.5 2.5 7.5 H3.5 C4.5 15.5 6 14 6 9 Z" fill="${f}"/>` +
    '<path d="M10 20 a2 2 0 0 0 4 0"/></svg>'
  );
}

// ----------------------------------------------------------------------
// Estimation-editor defaults (mirror the SPA's YieldBlock, but now seeded
// with the subject listing's own price/area from our lookup).
// ----------------------------------------------------------------------

function subjectArea(state: PanelState): number | null {
  return state.run?.input_spec?.area_m2 ?? state.listing?.area_m2 ?? null;
}

function defaultPrice(state: PanelState): number | null {
  const run = state.run;
  if (run?.input_purchase_price_czk != null) return run.input_purchase_price_czk;
  if (run?.estimate_kind === 'sale' && run.estimated_sale_price_czk != null) {
    return run.estimated_sale_price_czk;
  }
  return state.listing?.price_czk ?? null;
}

function defaultRent(state: PanelState): number | null {
  /* Prefer the comparables estimate; if it found nothing (thin market), fall
   * back to the MF reference rent so the yield calculator still works. */
  return state.run?.estimated_monthly_rent_czk
    ?? state.listing?.mf_reference_rent_czk
    ?? null;
}

function computeYield(state: PanelState): number | null {
  const { rent, costPerM2, price, renovation } = state;
  const area = subjectArea(state);
  const fond = costPerM2 != null && area != null ? costPerM2 * area : null;
  /* Total acquisition cost = listing price + one-off renovation budget. */
  const acquisition = price != null ? price + (renovation ?? 0) : null;
  if (rent == null || fond == null || acquisition == null || acquisition <= 0) {
    return null;
  }
  return ((rent - fond) * 12) / acquisition * 100;
}

/* The two derived quantities the yield is built from, shown as field hints so the
 * operator sees the formula's parts (mirrors the SPA's YieldBlock): the monthly
 * fond from the per-m² rate × area, and the acquisition denominator (price +
 * renovation). Empty fond hint (no area) collapses via `.field-hint:empty`. */
function fondHint(state: PanelState): string {
  const area = subjectArea(state);
  if (state.costPerM2 == null || area == null) return '';
  return `= ${fmtCzk(Math.round(state.costPerM2 * area))}/měs`;
}

function acquisitionHint(state: PanelState): string {
  const reno = state.renovation ?? 0;
  return reno > 0 && state.price != null
    ? `Akvizice ${fmtCzk(state.price + reno)}`
    : 'Jednorázový rozpočet, přičte se k ceně';
}

/* The two yield figures shown in the minimized bar: the precomputed MF gross
 * yield (sale apts) and the operator's live comparables yield (when an estimation
 * is loaded). Either may be absent — the bar degrades to whatever exists. */
function minimizedYieldCells(state: PanelState): { label: string; value: string }[] {
  const cells: { label: string; value: string }[] = [];
  const mf = state.listing?.mf_gross_yield_pct ?? null;
  if (state.isSaleApt !== false && mf != null) {
    cells.push({ label: 'MF', value: fmtPct(mf) });
  }
  if (state.run?.status === 'success') {
    const y = computeYield(state);
    if (y != null) cells.push({ label: 'Odhad', value: fmtPct(y) });
  }
  return cells;
}

/* The window-control glyphs: a minimize line (collapse to bar) + a restore
 * chevron (the bar grows upward into the full card). Inline SVG, currentColor. */
function minimizeIconSvg(): string {
  return (
    '<svg class="ctrl-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" ' +
    'stroke-width="1.6" stroke-linecap="round" aria-hidden="true">' +
    '<line x1="4" y1="11.5" x2="12" y2="11.5"/></svg>'
  );
}

function restoreIconSvg(): string {
  return (
    '<svg class="ctrl-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" ' +
    'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<polyline points="4.5,10 8,6.5 11.5,10"/></svg>'
  );
}

/* Toggle the persisted minimized preference + re-render the current panel. */
function onToggleMinimize(): void {
  minimized = !minimized;
  void chrome.storage.local.set({ [MINIMIZED_KEY]: minimized });
  render(state);
}

function bodyFromState(state: PanelState): YieldScenarioUpdate {
  return {
    rent_czk: state.rentTouched ? state.rent : null,
    fond_per_m2_czk: state.costTouched ? state.costPerM2 : null,
    price_czk: state.priceTouched ? state.price : null,
    renovation_czk: state.renovationTouched ? state.renovation : null,
  };
}

/* Load the editable scenario from a full estimation run. */
function seedFromRun(state: PanelState, run: EstimationRun): PanelState {
  const sc = run.scenario;
  const next: PanelState = { ...state, run };
  return {
    ...next,
    rentTouched: sc?.rent_czk != null,
    costTouched: sc?.fond_per_m2_czk != null,
    priceTouched: sc?.price_czk != null,
    renovationTouched: sc?.renovation_czk != null,
    rent: sc?.rent_czk ?? defaultRent(next),
    costPerM2: sc?.fond_per_m2_czk ?? DEFAULT_FOND_CZK_PER_M2,
    price: sc?.price_czk ?? defaultPrice(next),
    renovation: sc?.renovation_czk ?? DEFAULT_RENOVATION_CZK,
  };
}

// ----------------------------------------------------------------------
// Panel mount + render
// ----------------------------------------------------------------------

function mountPanel(): {
  shadow: ShadowRoot;
  render: (state: PanelState) => void;
  destroy: () => void;
} {
  const existing = document.getElementById(HOST_ELEMENT_ID);
  if (existing != null) existing.remove();

  const host = document.createElement('div');
  host.id = HOST_ELEMENT_ID;
  document.documentElement.appendChild(host);
  const shadow = host.attachShadow({ mode: 'closed' });
  const styleEl = document.createElement('style');
  styleEl.textContent = styles;
  shadow.appendChild(styleEl);

  const panel = document.createElement('div');
  panel.className = 'panel';
  shadow.appendChild(panel);

  let lastFocusedKey: 'rent' | 'cost' | 'price' | 'renovation' | 'note' | null = null;

  /* The ledger header band: a small copper index-mark + product wordmark, and
   * the close control. The wordmark is the shared product brand (APP_NAME) —
   * mirrors the SPA header — so it reads honestly for every listing, including
   * non-apartments where no yield is shown. */
  function header(showMinimize: boolean): HTMLElement {
    const h = document.createElement('div');
    h.className = 'p-head';
    const mark = document.createElement('div');
    mark.className = 'p-mark';
    const tick = document.createElement('span');
    tick.className = 'p-tick';
    mark.appendChild(tick);
    const word = document.createElement('span');
    word.className = 'p-word';
    word.textContent = APP_NAME;
    mark.appendChild(word);
    h.appendChild(mark);
    const controls = document.createElement('div');
    controls.className = 'p-controls';
    if (showMinimize) {
      const min = document.createElement('button');
      min.className = 'p-min';
      min.type = 'button';
      min.title = 'Minimalizovat';
      min.setAttribute('aria-label', 'Minimalizovat');
      min.innerHTML = minimizeIconSvg();
      min.onclick = () => onToggleMinimize();
      controls.appendChild(min);
    }
    const close = document.createElement('button');
    close.className = 'p-close';
    close.type = 'button';
    close.textContent = '×';
    close.title = 'Skrýt panel';
    close.onclick = () => host.remove();
    controls.appendChild(close);
    h.appendChild(controls);
    return h;
  }

  /* The minimized panel: a tiny one-line bar showing just the two yield figures
   * (the operator's at-a-glance triage signal), with a restore chevron + close.
   * Clicking anywhere on the bar (except ×) expands. Reads as the spine of the
   * same filed card — copper tick + stamped copper figures. */
  function renderMinimizedBar(state: PanelState): HTMLElement {
    const bar = document.createElement('div');
    bar.className = 'min-bar';
    bar.title = 'Rozbalit panel';
    bar.onclick = () => onToggleMinimize();

    const tick = document.createElement('span');
    tick.className = 'min-tick';
    bar.appendChild(tick);

    const content = document.createElement('div');
    content.className = 'min-content';
    if (state.phase === 'loading') {
      content.appendChild(minText('Načítám…'));
    } else if (state.phase === 'deactivated') {
      content.appendChild(minText('Není v databázi'));
    } else {
      const cells = minimizedYieldCells(state);
      if (cells.length > 0) {
        cells.forEach((c, i) => {
          if (i > 0) {
            const sep = document.createElement('span');
            sep.className = 'min-sep';
            sep.textContent = '·';
            content.appendChild(sep);
          }
          const cell = document.createElement('span');
          cell.className = 'min-cell';
          const lab = document.createElement('span');
          lab.className = 'min-label';
          lab.textContent = c.label;
          const val = document.createElement('span');
          val.className = 'min-value';
          val.textContent = c.value;
          cell.append(lab, val);
          content.appendChild(cell);
        });
      } else {
        /* No yields (non-sale-apt / not yet computed) → a compact subject. */
        const l = state.listing;
        const parts: string[] = [];
        const kind = l?.kind_label ?? l?.disposition;
        if (kind) parts.push(kind);
        if (l?.price_czk != null) parts.push(fmtCzk(l.price_czk));
        content.appendChild(minText(parts.join(' · ') || APP_NAME));
      }
    }
    bar.appendChild(content);

    const restore = document.createElement('span');
    restore.className = 'min-restore';
    restore.innerHTML = restoreIconSvg();
    bar.appendChild(restore);

    const close = document.createElement('button');
    close.className = 'min-close';
    close.type = 'button';
    close.textContent = '×';
    close.title = 'Skrýt panel';
    close.onclick = (e) => { e.stopPropagation(); host.remove(); };
    bar.appendChild(close);

    return bar;
  }

  function minText(text: string): HTMLElement {
    const s = document.createElement('span');
    s.className = 'min-fallback';
    s.textContent = text;
    return s;
  }

  const render = (state: PanelState): void => {
    panel.innerHTML = '';
    /* Minimized collapses every phase to the tiny bar EXCEPT error (a failure
     * should stay fully visible). The minimize control itself only shows once
     * there's a full panel worth collapsing (active phase). */
    const isMin = minimized && state.phase !== 'error';
    panel.classList.toggle('panel--min', isMin);
    panel.classList.toggle('panel--muted', state.phase === 'deactivated' && !isMin);

    if (isMin) {
      panel.appendChild(renderMinimizedBar(state));
      return;
    }

    panel.appendChild(header(state.phase === 'active'));
    const body = document.createElement('div');
    body.className = 'p-body';
    panel.appendChild(body);

    if (state.phase === 'loading') {
      body.appendChild(note('Načítám data…', 'note--loading'));
      return;
    }
    if (state.phase === 'signed_out') {
      renderSignedOut(body, state);
      return;
    }
    if (state.phase === 'error') {
      renderAuthStrip(body, state);
      body.appendChild(errorLine(state.errorMessage ?? 'Něco se nepovedlo.'));
      return;
    }
    if (state.phase === 'deactivated') {
      renderAuthStrip(body, state);
      body.appendChild(note('Tato nemovitost zatím není v naší databázi.'));
      return;
    }

    /* active — read top-down: sign-out control → WHAT it is (subject) → the
     * stamped MF yield → act on it (bookmark / open in app) → the deeper
     * estimate. MF + estimate are gated to apartments for sale; the subject +
     * actions are not. */
    renderAuthStrip(body, state);
    renderSubjectFacts(body, state);
    if (state.isSaleApt !== false) {
      renderMfBlock(body, state);
      renderActionsBar(body, state);
      renderEstimation(body, state);
    } else {
      renderActionsBar(body, state);
      body.appendChild(note('Výnos MF a odhad jsou jen u bytů na prodej.'));
    }
    renderNotes(body, state);  // any listing we have a property for
    if (state.errorMessage != null) body.appendChild(errorLine(state.errorMessage));

    /* Restore focus after a full re-render so typing isn't interrupted (the
     * estimation inputs AND the note textarea both carry data-key). */
    if (lastFocusedKey != null) {
      const target = shadow.querySelector<HTMLInputElement | HTMLTextAreaElement>(
        `[data-key="${lastFocusedKey}"]`,
      );
      if (target != null) {
        target.focus();
        const v = target.value;
        try { target.setSelectionRange(v.length, v.length); } catch { /* */ }
      }
    }
  };

  function note(text: string, variant = ''): HTMLElement {
    const p = document.createElement('p');
    p.className = 'note' + (variant ? ` ${variant}` : '');
    p.textContent = text;
    return p;
  }

  function errorLine(text: string): HTMLElement {
    const p = document.createElement('p');
    p.className = 'note note--error';
    p.textContent = text;
    return p;
  }

  /* The extension's own sign-in prompt (Wave 1) — every route now requires a
   * real Supabase session, so a signed-out operator sees this instead of a
   * raw 401. Google OAuth via chrome.identity.launchWebAuthFlow + PKCE, run
   * in the background worker (the only context that can open the auth
   * window and reach GoTrue). */
  function renderSignedOut(body: HTMLElement, state: PanelState): void {
    body.appendChild(note('Pro zobrazení dat se prosím přihlaste.'));
    if (state.errorMessage != null) body.appendChild(errorLine(state.errorMessage));
    const btn = document.createElement('button');
    btn.className = 'btn-primary';
    btn.type = 'button';
    btn.disabled = state.busy;
    btn.textContent = state.busy ? 'Přihlašuji…' : 'Přihlásit se přes Google';
    btn.onclick = () => { void onSignIn(); };
    body.appendChild(btn);
  }

  /* The signed-in operator's identity + a sign-out control — shown atop the
   * panel body whenever a session is loaded (active/deactivated/error), so
   * signing out is always one click away without a separate popup surface. */
  function renderAuthStrip(body: HTMLElement, state: PanelState): void {
    if (state.authEmail == null) return;
    const strip = document.createElement('div');
    strip.className = 'auth-strip';
    const email = document.createElement('span');
    email.className = 'auth-email';
    email.textContent = state.authEmail;
    email.title = state.authEmail;
    strip.appendChild(email);
    const out = document.createElement('a');
    out.className = 'auth-signout';
    out.textContent = 'Odhlásit';
    out.href = '#';
    out.onclick = (e) => { e.preventDefault(); void onSignOut(); };
    strip.appendChild(out);
    body.appendChild(strip);
  }

  /* The two "this listing ↔ our app" affordances in one row: the deal-pipeline
   * bookmark (left) + the "open in our app" deep-link (right). Either may be
   * absent (no property yet / no SPA base configured); the row appears only if
   * something landed in it. */
  function renderActionsBar(body: HTMLElement, state: PanelState): void {
    const row = document.createElement('div');
    row.className = 'actions-bar';
    renderPipelineToggle(row, state);       // the funnel — sole pipeline affordance (rule #22)
    renderMonitoringToggle(row, state);     // separate, adjacent collections/monitoring control
    renderAppLink(row, state);
    if (row.childElementCount > 0) body.appendChild(row);
  }

  function renderAppLink(container: HTMLElement, state: PanelState): void {
    const l = state.listing;
    // `found` is the DB-membership flag the lookup already returns. Gating on
    // sreality_id != null would hide the app link for every post-Gate-2 listing,
    // which HAS an app page — and the link below never uses that id anyway.
    if (!l || !l.found || !APP_BASE_URL) return;
    const a = document.createElement('a');
    a.className = 'app-link';
    // Canonical natural-key URL, never the negative synthetic id (migration 097):
    // the extension already holds the (source, native id) it looked the listing up
    // by, which is exactly the app's /listing/{source}/{native} route.
    a.href =
      `${APP_BASE_URL}/listing/${encodeURIComponent(l.source)}/${encodeURIComponent(l.source_id)}`;
    a.target = '_blank';
    a.rel = 'noopener';
    const txt = document.createElement('span');
    txt.textContent = 'Otevřít v aplikaci';
    const arrow = document.createElement('span');
    arrow.className = 'app-link-arrow';
    arrow.textContent = '→';
    a.append(txt, ' ', arrow);
    container.appendChild(a);
  }

  /* Deal-pipeline control for the listing's property (rule #22). Mirrors the
   * SPA's PipelineToggle: out of pipeline → a copper "Přidat do pipeline" button
   * (add → entry stage); in pipeline → a filled pill with a stage `<select>` (the
   * app's single-choice control — change stage via the SAME audited move PATCH)
   * + a `✕` to remove. Property-grain — shown for ANY listing we have a property
   * for, hidden during the brief window a freshly-scraped row has no property_id. */
  function renderPipelineToggle(container: HTMLElement, state: PanelState): void {
    const l = state.listing;
    if (l == null || !l.found || l.property_id == null) return;
    const inPipe = l.pipeline?.in_pipeline ?? false;

    if (!inPipe) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'pipeline-toggle';
      btn.disabled = state.pipelineBusy;
      btn.setAttribute('aria-pressed', 'false');
      btn.title = 'Přidat do pipeline';
      btn.innerHTML = funnelIconSvg(false);
      const label = document.createElement('span');
      label.textContent = 'Přidat do pipeline';
      btn.appendChild(label);
      btn.onclick = () => { void onTogglePipeline(); };
      container.appendChild(btn);
      return;
    }

    const pill = document.createElement('div');
    pill.className = 'pipeline-pill' + (state.pipelineBusy ? ' pipeline-pill--busy' : '');

    const icon = document.createElement('span');
    icon.className = 'pipeline-pill-icon';
    icon.innerHTML = funnelIconSvg(true);
    pill.appendChild(icon);

    /* The stage selector. Options come from the loaded stage list; until it
     * arrives, a single option (the current stage) keeps the label visible. */
    const select = document.createElement('select');
    select.className = 'pipeline-select';
    select.disabled = state.pipelineBusy;
    select.title = 'Změnit fázi';
    select.setAttribute('aria-label', 'Fáze v pipeline');
    const stages = state.stages
      ?? (l.pipeline?.stage_id != null
        ? [{ id: l.pipeline.stage_id, label: l.pipeline.stage_label ?? 'V pipeline' }]
        : []);
    if (state.stages == null) select.disabled = true;  // not loaded yet
    for (const s of stages) {
      const opt = document.createElement('option');
      opt.value = String(s.id);
      opt.textContent = s.label;
      if (s.id === l.pipeline?.stage_id) opt.selected = true;
      select.appendChild(opt);
    }
    select.onchange = () => { void onMoveStage(Number(select.value)); };
    pill.appendChild(select);

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'pipeline-remove';
    remove.disabled = state.pipelineBusy;
    remove.title = 'Odebrat z pipeline';
    remove.setAttribute('aria-label', 'Odebrat z pipeline');
    remove.textContent = '✕';
    remove.onclick = () => { void onTogglePipeline(); };
    pill.appendChild(remove);

    container.appendChild(pill);
  }

  /* Collections / monitoring control for the listing's property (rule #18) — a
   * SEPARATE, adjacent affordance to the pipeline funnel (rule #22: the funnel
   * is the sole pipeline control, never merged with this). One-click monitoring:
   * out of the monitoring collection → a "Sledovat" bell button; in → a filled
   * "Sledováno" pill with a ✕ to stop. Property-grain, shown only when we have a
   * property AND the operator has a monitoring collection set up. */
  function renderMonitoringToggle(container: HTMLElement, state: PanelState): void {
    const l = state.listing;
    if (l == null || !l.found || l.property_id == null) return;
    const target = monitoringTarget(state.collections);
    if (target == null) return;  // not loaded yet, or no monitoring collection
    const inColl = l.collection_ids?.includes(target.id) ?? false;

    if (!inColl) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'collection-toggle';
      btn.disabled = state.collectionBusy;
      btn.setAttribute('aria-pressed', 'false');
      btn.title = `Sledovat (${target.name})`;
      btn.innerHTML = bellIconSvg(false);
      const label = document.createElement('span');
      label.textContent = 'Sledovat';
      btn.appendChild(label);
      btn.onclick = () => { void onToggleMonitoring(); };
      container.appendChild(btn);
      return;
    }

    const pill = document.createElement('div');
    pill.className = 'collection-pill' + (state.collectionBusy ? ' collection-pill--busy' : '');
    pill.title = `Sledováno (${target.name})`;

    const icon = document.createElement('span');
    icon.className = 'collection-pill-icon';
    icon.innerHTML = bellIconSvg(true);
    pill.appendChild(icon);

    const label = document.createElement('span');
    label.className = 'collection-pill-label';
    label.textContent = 'Sledováno';
    pill.appendChild(label);

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'collection-remove';
    remove.disabled = state.collectionBusy;
    remove.title = 'Přestat sledovat';
    remove.setAttribute('aria-label', 'Přestat sledovat');
    remove.textContent = '✕';
    remove.onclick = () => { void onToggleMonitoring(); };
    pill.appendChild(remove);

    container.appendChild(pill);
  }

  /* Operator notes for the listing's property (rule #18) — shown for ANY listing
   * we have a property for: the existing notes (most-recent-first) + an add box.
   * A saved note carries the viewed advert as origin provenance. */
  function renderNotes(body: HTMLElement, state: PanelState): void {
    const l = state.listing;
    if (l == null || !l.found || l.property_id == null) return;
    const sec = document.createElement('div');
    sec.className = 'notes';

    const eyebrow = document.createElement('p');
    eyebrow.className = 'notes-eyebrow';
    const count = state.notes?.length ?? 0;
    eyebrow.textContent = count > 0 ? `Poznámky (${count})` : 'Poznámky';
    sec.appendChild(eyebrow);

    if (state.notes != null && state.notes.length > 0) {
      const list = document.createElement('div');
      list.className = 'notes-list';
      for (const n of state.notes) {
        const item = document.createElement('div');
        item.className = 'note-item';
        const bodyEl = document.createElement('p');
        bodyEl.className = 'note-body';
        bodyEl.textContent = n.body;
        const date = document.createElement('span');
        date.className = 'note-date';
        date.textContent = fmtNoteDate(n.created_at);
        date.title = n.created_at;
        item.appendChild(bodyEl);
        item.appendChild(date);
        list.appendChild(item);
      }
      sec.appendChild(list);
    }

    const ta = document.createElement('textarea');
    ta.className = 'note-input';
    ta.rows = 2;
    ta.maxLength = 4000;  // mirror the server's CreateNoteIn cap + the SPA's textarea
    ta.placeholder = 'Přidat poznámku…';
    ta.value = noteDraft;
    ta.dataset.key = 'note';
    ta.disabled = state.noteBusy;
    ta.addEventListener('focus', () => { lastFocusedKey = 'note'; });
    ta.addEventListener('blur', () => { if (lastFocusedKey === 'note') lastFocusedKey = null; });
    ta.addEventListener('input', (e) => {
      noteDraft = (e.target as HTMLTextAreaElement).value;
    });
    sec.appendChild(ta);

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn-primary note-save';
    btn.textContent = state.noteBusy ? 'Ukládám…' : 'Uložit poznámku';
    btn.disabled = state.noteBusy;
    btn.onclick = () => { void onAddNote(); };
    sec.appendChild(btn);

    body.appendChild(sec);
  }

  /* The stamped valuation: eyebrow → big copper yield figure → a hairline-ruled
   * ledger line carrying the MF reference rent (the signature of the panel). */
  function renderMfBlock(body: HTMLElement, state: PanelState): void {
    const l = state.listing;
    const mf = document.createElement('div');
    mf.className = 'mf';

    const eyebrow = document.createElement('p');
    eyebrow.className = 'mf-eyebrow';
    eyebrow.textContent = 'Výnos MF (hrubý)';
    eyebrow.title = 'Hrubý výnos dle cenové mapy nájemného MF (nájem ÷ cena)';
    mf.appendChild(eyebrow);

    const pct = l?.mf_gross_yield_pct ?? null;
    const figure = document.createElement('p');
    figure.className = 'mf-figure' + (pct == null ? ' mf-figure--muted' : '');
    figure.textContent = fmtPct(pct);
    mf.appendChild(figure);

    if (l?.mf_reference_rent_czk != null) {
      const area = l.area_m2 ?? null;
      const perM2 = area && area > 0 ? Math.round(l.mf_reference_rent_czk / area) : null;
      const ledger = document.createElement('div');
      ledger.className = 'mf-ledger';
      const lab = document.createElement('span');
      lab.className = 'mf-ledger-label';
      lab.textContent = 'MF nájem';
      const val = document.createElement('span');
      val.className = 'mf-ledger-value';
      val.textContent =
        `${fmtCzk(l.mf_reference_rent_czk)}/měs` +
        (perM2 != null ? ` · ${perM2.toLocaleString('cs-CZ')} Kč/m²` : '');
      ledger.appendChild(lab);
      ledger.appendChild(val);
      mf.appendChild(ledger);
    } else if (l?.found) {
      mf.appendChild(note('MF nájem nedostupný (chybí území nebo cena).'));
    } else {
      mf.appendChild(note('Tato nemovitost zatím není v naší databázi.'));
    }
    body.appendChild(mf);
  }

  /* The catalog subject line: the disposition as the headword, area + price as
   * tabular metadata, the place on a quiet second line. Left-aligned, like an
   * archive entry — grounds the operator before the yield figure. */
  function renderSubjectFacts(body: HTMLElement, state: PanelState): void {
    const l = state.listing;
    if (l == null || !l.found) return;
    const subject = document.createElement('div');
    subject.className = 'subject';

    const line = document.createElement('div');
    line.className = 'subject-line';
    const kind = l.kind_label ?? l.disposition;
    if (kind) {
      const disp = document.createElement('span');
      disp.className = 'subject-disp';
      disp.textContent = kind;
      line.appendChild(disp);
    }
    const meta: string[] = [];
    if (l.area_m2 != null) meta.push(`${Math.round(l.area_m2)} m²`);
    if (l.price_czk != null) meta.push(fmtCzk(l.price_czk));
    if (meta.length > 0) {
      const m = document.createElement('span');
      m.className = 'subject-meta';
      m.textContent = meta.join(' · ');
      line.appendChild(m);
    }
    if (line.childElementCount > 0) subject.appendChild(line);

    const place = l.district ?? l.locality;
    if (place) {
      const p = document.createElement('p');
      p.className = 'subject-place';
      p.textContent = place;
      subject.appendChild(p);
    }
    if (subject.childElementCount > 0) body.appendChild(subject);
  }

  function renderEstimation(body: HTMLElement, state: PanelState): void {
    const sec = document.createElement('div');
    sec.className = 'est';
    const head = document.createElement('p');
    head.className = 'est-eyebrow';
    head.textContent = 'Odhad výnosu (komparativní)';
    sec.appendChild(head);

    /* No estimation yet (or one is running). */
    if (state.run == null) {
      if (state.busy) {
        sec.appendChild(note('Odhad probíhá… (~10–30 s)', 'note--loading'));
      } else {
        const btn = document.createElement('button');
        btn.className = 'btn-primary';
        btn.type = 'button';
        btn.textContent = 'Spustit odhad';
        btn.onclick = () => onCreateRun();
        sec.appendChild(btn);
      }
      body.appendChild(sec);
      return;
    }

    /* A failed run loaded/just-completed — show the reason + a retry. */
    if (state.run.status === 'failed') {
      sec.appendChild(errorLine(state.run.error_message ?? 'Odhad selhal.'));
      const btn = document.createElement('button');
      btn.className = 'btn-primary';
      btn.type = 'button';
      btn.textContent = state.busy ? 'Počítám…' : 'Spustit znovu';
      btn.disabled = state.busy;
      btn.onclick = () => onCreateRun();
      sec.appendChild(btn);
      body.appendChild(sec);
      return;
    }

    /* Success — surface what kind of result it is. A 'rent' run with no rent
     * means the comparables search found nothing nearby (thin market): we fall
     * the calculator back to the MF rent and say so. */
    if (state.run.estimate_kind === 'rent' && state.run.estimated_monthly_rent_czk == null) {
      sec.appendChild(note(
        'Bez srovnatelných nájmů v okolí — počítáno z MF nájmu (uprav dle potřeby).',
      ));
    } else if (state.run.confidence === 'low') {
      sec.appendChild(note('Nízká spolehlivost odhadu (málo srovnatelných).'));
    }

    const fields = document.createElement('div');
    fields.className = 'fields';
    fields.appendChild(buildField({
      key: 'rent', label: 'Měsíční nájem', suffix: 'Kč',
      value: state.rent, onInput: (v) => onEdit('rent', v),
    }));
    fields.appendChild(buildField({
      key: 'cost', label: 'Fond oprav + SVJ', suffix: 'Kč/m²',
      value: state.costPerM2, onInput: (v) => onEdit('cost', v),
      hint: fondHint(state),
    }));
    fields.appendChild(buildField({
      key: 'price', label: 'Cena', suffix: 'Kč',
      value: state.price, onInput: (v) => onEdit('price', v),
    }));
    fields.appendChild(buildField({
      key: 'renovation', label: 'Rekonstrukce', suffix: 'Kč',
      value: state.renovation, onInput: (v) => onEdit('renovation', v),
      hint: acquisitionHint(state),
    }));
    sec.appendChild(fields);

    const yieldRow = document.createElement('div');
    yieldRow.className = 'est-yield';
    const yl = document.createElement('span');
    yl.className = 'est-yield-label';
    yl.textContent = 'Výnos z odhadu';
    const yv = document.createElement('span');
    yv.className = 'est-yield-value';
    yv.textContent = fmtPct(computeYield(state));
    yieldRow.appendChild(yl);
    yieldRow.appendChild(yv);
    sec.appendChild(yieldRow);

    const foot = document.createElement('div');
    foot.className = 'est-foot';
    const status = document.createElement('span');
    status.className = 'est-status';
    const hasOverrides =
      state.rentTouched || state.costTouched || state.priceTouched
      || state.renovationTouched;
    status.textContent = hasOverrides ? 'upraveno · uloženo' : 'živý výpočet';
    foot.appendChild(status);
    /* Reset is always present (toggled), so onEdit can reveal it in place
     * without a full re-render — see onEdit / overridesChanged. */
    const reset = document.createElement('a');
    reset.className = 'est-reset';
    reset.textContent = 'Reset';
    reset.style.display = hasOverrides ? '' : 'none';
    reset.onclick = (e) => { e.preventDefault(); onReset(); };
    foot.appendChild(reset);
    sec.appendChild(foot);
    body.appendChild(sec);
  }

  function buildField(opts: {
    key: 'rent' | 'cost' | 'price' | 'renovation';
    label: string;
    suffix: string;
    value: number | null;
    onInput: (v: number | null) => void;
    hint?: string;
  }): HTMLElement {
    const wrap = document.createElement('div');
    wrap.className = 'field';
    const label = document.createElement('label');
    label.className = 'field-label';
    label.textContent = opts.label;
    wrap.appendChild(label);
    const row = document.createElement('div');
    row.className = 'field-row';
    const input = document.createElement('input');
    input.type = 'text';
    input.inputMode = 'decimal';
    input.className = 'field-input';
    input.value = formatNumber(opts.value);
    input.dataset.key = opts.key;
    input.addEventListener('focus', () => { lastFocusedKey = opts.key; });
    input.addEventListener('blur', () => {
      if (lastFocusedKey === opts.key) lastFocusedKey = null;
    });
    input.addEventListener('input', (e) => {
      opts.onInput(parseNumber((e.target as HTMLInputElement).value));
    });
    row.appendChild(input);
    const suf = document.createElement('span');
    suf.className = 'field-suffix';
    suf.textContent = opts.suffix;
    row.appendChild(suf);
    wrap.appendChild(row);
    if (opts.hint !== undefined) {
      const h = document.createElement('p');
      h.className = 'field-hint';
      h.dataset.hintFor = opts.key;  // onEdit refreshes it in place (no re-render)
      h.textContent = opts.hint;
      wrap.appendChild(h);
    }
    return wrap;
  }

  return { shadow, render, destroy: () => host.remove() };
}

// ----------------------------------------------------------------------
// App-level state machine
// ----------------------------------------------------------------------

let state: PanelState;
let render: (s: PanelState) => void;
let panelShadow: ShadowRoot | null = null;
/* The listing URL the panel currently represents — drives create_estimation.
 * On a detail page it's location.href; opened from an index card it's that
 * card's detail href (NOT the search page). */
let panelUrl = '';
/* The same listing's portal ref, kept alongside panelUrl so onSignIn can
 * re-run openPanel() once a session lands, without the caller re-supplying it. */
let panelRef: PortalRef | null = null;
let patchTimer: ReturnType<typeof setTimeout> | null = null;

function setState(updater: (prev: PanelState) => PanelState): void {
  state = updater(state);
  render(state);
}

function schedulePatch(): void {
  if (patchTimer != null) clearTimeout(patchTimer);
  patchTimer = setTimeout(async () => {
    patchTimer = null;
    if (state.run == null) return;
    const res = await call<EstimationRun>({
      type: 'patch_scenario', run_id: state.run.id, body: bodyFromState(state),
    });
    if (!res.ok) {
      setState((prev) => ({ ...prev, errorMessage: `Uložení selhalo: ${friendlyDetail(res.detail)}` }));
      return;
    }
    /* Save succeeded — nothing visible depends on the refreshed run, so update
     * state silently. A full re-render here would destroy the <input> the
     * operator is typing in and steal focus (the bug we're avoiding). */
    state.run = res.data;
    if (state.errorMessage != null) {
      state.errorMessage = null;
      render(state);
    }
  }, PATCH_DEBOUNCE_MS);
}

/* Edits update state + the derived display IN PLACE — never a full re-render,
 * which would rebuild the inputs and drop focus mid-keystroke. */
function onEdit(
  axis: 'rent' | 'cost' | 'price' | 'renovation', value: number | null,
): void {
  switch (axis) {
    case 'rent': state.rent = value; state.rentTouched = true; break;
    case 'cost': state.costPerM2 = value; state.costTouched = true; break;
    case 'price': state.price = value; state.priceTouched = true; break;
    case 'renovation':
      state.renovation = value; state.renovationTouched = true; break;
  }
  const yv = panelShadow?.querySelector<HTMLElement>('.est-yield-value');
  if (yv != null) yv.textContent = fmtPct(computeYield(state));
  /* Refresh the derived field hints (fond/měs + acquisition denominator) in
   * place too, so the formula's parts track the inputs without a re-render. */
  const fh = panelShadow?.querySelector<HTMLElement>('[data-hint-for="cost"]');
  if (fh != null) fh.textContent = fondHint(state);
  const ah = panelShadow?.querySelector<HTMLElement>('[data-hint-for="renovation"]');
  if (ah != null) ah.textContent = acquisitionHint(state);
  const status = panelShadow?.querySelector<HTMLElement>('.est-status');
  if (status != null) status.textContent = 'upraveno · uloženo';
  const reset = panelShadow?.querySelector<HTMLElement>('.est-reset');
  if (reset != null) reset.style.display = '';
  schedulePatch();
}

function onReset(): void {
  setState((prev) => ({
    ...prev,
    rent: defaultRent(prev),
    costPerM2: DEFAULT_FOND_CZK_PER_M2,
    price: defaultPrice(prev),
    renovation: DEFAULT_RENOVATION_CZK,
    rentTouched: false, costTouched: false, priceTouched: false,
    renovationTouched: false,
  }));
  schedulePatch();
}

async function onCreateRun(): Promise<void> {
  setState((prev) => ({ ...prev, busy: true, errorMessage: null }));
  const res = await call<EstimationRun>({
    type: 'create_estimation', url: panelUrl,
  });
  if (!res.ok) {
    setState((prev) => ({
      ...prev, busy: false, errorMessage: `Odhad se nepodařilo spustit: ${friendlyDetail(res.detail)}`,
    }));
    return;
  }
  let row = res.data;
  for (let i = 0; i < POLL_MAX_ATTEMPTS; i++) {
    if (row.status === 'success' || row.status === 'failed') break;
    await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
    const next = await call<EstimationRun>({ type: 'get_estimation', run_id: row.id });
    if (!next.ok) break;
    row = next.data;
  }
  if (row.status !== 'success') {
    setState((prev) => ({
      ...prev, busy: false,
      errorMessage: row.error_message ?? 'Odhad se nedokončil včas.',
    }));
    return;
  }
  setState((prev) => seedFromRun({ ...prev, busy: false }, row));
}

/* Bookmark / un-bookmark the listing's property into the deal pipeline. The
 * UI flips optimistically (one re-render), then reconciles from the server:
 * an add returns the entry-stage label to display; a remove clears membership.
 * On failure we revert and surface the reason. Reuses the SAME bearer-gated
 * /pipeline/cards endpoints the SPA writes through. */
/* Apply a pipeline-membership update only if the panel STILL represents the
 * property the write was started for. The panel state is a single module global
 * that openPanel() replaces wholesale — re-opening for a different card (an index
 * badge, a MutationObserver re-pass) mid-request would otherwise bleed this
 * listing's result onto that one. Network writes target the captured propertyId
 * regardless; this guard only protects the display. */
function applyMembershipIf(
  propertyId: number, membership: PortalListing['pipeline'], patch: Partial<PanelState>,
): (prev: PanelState) => PanelState {
  return (prev) =>
    prev.listing?.property_id === propertyId
      ? withPipeline({ ...prev, ...patch }, membership)
      : prev;
}

async function onTogglePipeline(): Promise<void> {
  const l = state.listing;
  if (l == null || l.property_id == null) return;
  const propertyId = l.property_id;
  const wasIn = l.pipeline?.in_pipeline ?? false;
  const prior = l.pipeline;

  setState(applyMembershipIf(
    propertyId,
    { in_pipeline: !wasIn, stage_id: null, stage_key: null, stage_label: null },
    { pipelineBusy: true, errorMessage: null },
  ));

  const res = await call<PipelineCardResult>({
    type: wasIn ? 'remove_pipeline_card' : 'add_pipeline_card',
    property_id: propertyId,
  });

  if (!res.ok) {
    setState(applyMembershipIf(
      propertyId,
      wasIn
        ? { in_pipeline: true, stage_id: prior?.stage_id ?? null,
            stage_key: prior?.stage_key ?? null, stage_label: prior?.stage_label ?? null }
        : { in_pipeline: false, stage_id: null, stage_key: null, stage_label: null },
      { pipelineBusy: false, errorMessage: `Uložení do pipeline selhalo: ${friendlyDetail(res.detail)}` },
    ));
    return;
  }

  setState(applyMembershipIf(
    propertyId,
    wasIn
      ? { in_pipeline: false, stage_id: null, stage_key: null, stage_label: null }
      : {
          in_pipeline: true,
          stage_id: res.data.stage_id ?? null,
          stage_key: res.data.stage_key ?? null,
          stage_label: res.data.stage_label ?? null,
        },
    { pipelineBusy: false },
  ));
}

/* Apply a collection-membership update only if the panel STILL represents the
 * property the write was started for — the same identity guard as the pipeline
 * toggle (the panel state is a module global that openPanel replaces wholesale,
 * so a re-open for a different card mid-request must not bleed this result). */
function applyCollectionsIf(
  propertyId: number,
  collection_ids: PortalListing['collection_ids'],
  patch: Partial<PanelState>,
): (prev: PanelState) => PanelState {
  return (prev) =>
    prev.listing?.property_id === propertyId
      ? withCollections({ ...prev, ...patch }, collection_ids)
      : prev;
}

/* Add / remove the listing's property to/from the monitoring collection (rule
 * #18). Optimistic flip → reconcile from the server; revert + surface the reason
 * on failure. Mirrors onTogglePipeline's flow + error handling. Writes through
 * the SAME bearer-gated /collections/{id}/properties routes the SPA uses. */
async function onToggleMonitoring(): Promise<void> {
  const l = state.listing;
  if (l == null || l.property_id == null) return;
  const target = monitoringTarget(state.collections);
  if (target == null) return;
  const propertyId = l.property_id;
  const prior = l.collection_ids ?? [];
  const wasIn = prior.includes(target.id);
  const optimistic = wasIn
    ? prior.filter((id) => id !== target.id)
    : [...prior, target.id];

  setState(applyCollectionsIf(
    propertyId, optimistic, { collectionBusy: true, errorMessage: null },
  ));

  const res = await call<CollectionWriteResult>({
    type: wasIn ? 'remove_from_collection' : 'add_to_collection',
    collection_id: target.id,
    property_id: propertyId,
  });

  if (!res.ok) {
    setState(applyCollectionsIf(
      propertyId, prior,
      { collectionBusy: false, errorMessage: `Sledování se nepodařilo uložit: ${friendlyDetail(res.detail)}` },
    ));
    return;
  }

  setState(applyCollectionsIf(propertyId, optimistic, { collectionBusy: false }));
}

/* Change the deal stage of the in-pipeline property. The SAME audited PATCH the
 * SPA's PipelineToggle + the kanban use (stamps `entered_stage_at`, logs a
 * `moved` event). Optimistic: recolour/reselect from the chosen stage, then
 * reconcile from the server; revert on failure. Identity-guarded like the toggle. */
async function onMoveStage(stageId: number): Promise<void> {
  const l = state.listing;
  if (l == null || l.property_id == null || l.pipeline == null) return;
  if (l.pipeline.stage_id === stageId) return;  // no-op (already there)
  const propertyId = l.property_id;
  const prior = l.pipeline;
  const target = state.stages?.find((s) => s.id === stageId) ?? null;

  setState(applyMembershipIf(
    propertyId,
    {
      in_pipeline: true, stage_id: stageId,
      stage_key: target?.key ?? prior.stage_key,
      stage_label: target?.label ?? prior.stage_label,
    },
    { pipelineBusy: true, errorMessage: null },
  ));

  const res = await call<PipelineCardResult>({
    type: 'move_pipeline_card', property_id: propertyId, stage_id: stageId,
  });

  if (!res.ok) {
    setState(applyMembershipIf(
      propertyId, prior,
      { pipelineBusy: false, errorMessage: `Změna fáze selhala: ${friendlyDetail(res.detail)}` },
    ));
    return;
  }

  setState(applyMembershipIf(
    propertyId,
    {
      in_pipeline: true,
      stage_id: res.data.stage_id ?? stageId,
      stage_key: res.data.stage_key ?? target?.key ?? prior.stage_key,
      stage_label: res.data.stage_label ?? target?.label ?? prior.stage_label,
    },
    { pipelineBusy: false },
  ));
}

/* The operator-curated stage list, loaded once per page and cached at module
 * scope (stages change rarely — same staleness posture as the SPA's 60s cache).
 * Reused across panel re-opens so changing cards never re-fetches. A transient
 * failure retries a few times (bounded) so a single network blip can't strip
 * stage-changing for the page view; a persistent failure leaves the select on its
 * current-stage fallback and self-heals on the next panel open. */
let cachedStages: PipelineStage[] | null = null;
let stagesLoading = false;

async function loadStages(): Promise<void> {
  if (cachedStages != null) {
    if (state.stages == null) setState((prev) => ({ ...prev, stages: cachedStages }));
    return;
  }
  if (stagesLoading) return;  // a load is already in flight — dedupe across opens
  stagesLoading = true;
  try {
    for (let attempt = 0; attempt < 3; attempt++) {
      const res = await call<PipelineStage[]>({ type: 'list_pipeline_stages' });
      if (res.ok) {
        cachedStages = res.data;
        setState((prev) => ({ ...prev, stages: cachedStages }));
        return;
      }
      await new Promise((r) => setTimeout(r, 1500));  // transient blip → back off + retry
    }
  } finally {
    stagesLoading = false;
  }
}

/* The operator-curated collection list — loaded once per page, cached at module
 * scope and reused across panel re-opens, exactly like loadStages above (the
 * monitoring toggle needs it to pick a target collection). Same bounded retry +
 * self-heal-on-next-open posture. */
let cachedCollections: ExtCollection[] | null = null;
let collectionsLoading = false;

async function loadCollections(): Promise<void> {
  if (cachedCollections != null) {
    if (state.collections == null) {
      setState((prev) => ({ ...prev, collections: cachedCollections }));
    }
    return;
  }
  if (collectionsLoading) return;  // a load is already in flight — dedupe across opens
  collectionsLoading = true;
  try {
    for (let attempt = 0; attempt < 3; attempt++) {
      const res = await call<ExtCollection[]>({ type: 'list_collections' });
      if (res.ok) {
        cachedCollections = res.data;
        setState((prev) => ({ ...prev, collections: cachedCollections }));
        return;
      }
      await new Promise((r) => setTimeout(r, 1500));  // transient blip → back off + retry
    }
  } finally {
    collectionsLoading = false;
  }
}

/* The note add-box draft. A module global (not panel state) so editing it never
 * triggers a re-render — the textarea reads it on render, writes it on input;
 * reset per panel open so one listing's draft never bleeds onto another. */
let noteDraft = '';

/* Notes are PER-PROPERTY (not global like stages/collections), so they're fetched
 * fresh per panel open and held in panel state — never module-cached. The dedup
 * is the per-panel `state.notes == null` guard, NOT a page-wide in-flight flag: a
 * page-wide flag would early-return (and strand) a DIFFERENT property opened while
 * the first's fetch is still in flight (stages/collections survive that only via
 * their module cache re-apply, which notes deliberately don't have). A duplicate
 * fetch from a rapid same-property re-open is harmless (idempotent, identity-
 * guarded apply). Retried on a transient blip; identity-guarded so a mid-flight
 * panel re-open can't apply one property's notes onto another. */
async function loadNotes(): Promise<void> {
  const l = state.listing;
  if (l == null || l.property_id == null || state.notes != null) return;
  const propertyId = l.property_id;
  for (let attempt = 0; attempt < 3; attempt++) {
    const res = await call<ExtNote[]>({ type: 'list_notes', property_id: propertyId });
    if (res.ok) {
      setState((prev) =>
        prev.listing?.property_id === propertyId ? { ...prev, notes: res.data } : prev);
      return;
    }
    await new Promise((r) => setTimeout(r, 1500));
  }
}

/* Save the draft as a note on the listing's property. `origin_listing_id` is the
 * viewed advert's sreality_id (display provenance, rule #18). The new note is
 * prepended (the list is most-recent-first); the draft clears. Identity-guarded
 * like the other writes; reuses the SAME POST the SPA's CurationBlock uses. */
async function onAddNote(): Promise<void> {
  const l = state.listing;
  if (l == null || l.property_id == null) return;
  const body = noteDraft.trim();
  if (body === '') return;
  const propertyId = l.property_id;
  setState((prev) => ({ ...prev, noteBusy: true, errorMessage: null }));
  const res = await call<ExtNote>({
    // Send the surrogate: property_notes.origin_listing_id is the legacy handle
    // and is NULL for a post-Gate-2 listing, which would silently drop the
    // note's provenance. The API derives the legacy value from this.
    type: 'add_note', property_id: propertyId, body,
    origin_listing_ref_id: l.listing_id ?? null,
  });
  if (!res.ok) {
    setState((prev) => ({
      ...prev, noteBusy: false,
      errorMessage: `Uložení poznámky selhalo: ${friendlyDetail(res.detail)}`,
    }));
    return;
  }
  // Clear the draft only if the panel still shows this property — identity-guarded
  // like the prepend, so a mid-request re-open can't wipe another listing's draft.
  if (state.listing?.property_id === propertyId) noteDraft = '';
  setState((prev) =>
    prev.listing?.property_id === propertyId
      ? { ...prev, noteBusy: false, notes: [res.data, ...(prev.notes ?? [])] }
      : { ...prev, noteBusy: false });
}

/* Trigger the extension's own PKCE sign-in (Wave 1) — the background worker
 * owns chrome.identity (a content script can't call it directly), so this
 * just relays a message and re-runs openPanel() on success to reload
 * whatever the signed-out prompt was blocking. */
async function onSignIn(): Promise<void> {
  if (panelRef == null) return;
  const ref = panelRef;
  setState((prev) => ({ ...prev, busy: true, errorMessage: null }));
  const res = await call<undefined>({ type: 'sign_in' });
  if (!res.ok) {
    setState((prev) => ({
      ...prev, busy: false, phase: 'signed_out',
      errorMessage: `Přihlášení selhalo: ${res.detail}`,
    }));
    return;
  }
  await openPanel(ref, panelUrl);
}

async function onSignOut(): Promise<void> {
  await call<undefined>({ type: 'sign_out' });
  setState((prev) => ({
    ...prev, phase: 'signed_out', authEmail: null, listing: null, errorMessage: null,
  }));
}

/* Mounts/refreshes the floating panel for one listing. Used by the detail-page
 * entry AND by index-card badges (which pass the card's ref + href + the
 * already-fetched listing so no second lookup is needed). */
export async function openPanel(
  ref: PortalRef, url: string, prefetched?: PortalListing | null,
): Promise<void> {
  panelUrl = url;
  panelRef = ref;
  const panel = mountPanel();
  render = panel.render;
  panelShadow = panel.shadow;
  noteDraft = '';  // a fresh panel starts with an empty note draft
  state = {
    phase: 'loading', authEmail: null, listing: null, isSaleApt: null, run: null,
    rentTouched: false, costTouched: false, priceTouched: false,
    renovationTouched: false,
    rent: null, costPerM2: null, price: null, renovation: null, busy: false,
    pipelineBusy: false, stages: cachedStages,
    collections: cachedCollections, collectionBusy: false,
    notes: null, noteBusy: false, errorMessage: null,
  };
  await minimizedReady;  // persisted minimized pref before first paint → no flash
  render(state);

  /* Every route needs a real session now (Wave 1) — check first so a
   * signed-out operator sees one clean prompt instead of a lookup 401. */
  const auth = await call<AuthState>({ type: 'get_auth_state' });
  const authEmail = auth.ok && auth.data.signedIn ? auth.data.email : null;
  if (!auth.ok || !auth.data.signedIn) {
    setState((prev) => ({ ...prev, phase: 'signed_out', authEmail: null }));
    return;
  }
  setState((prev) => ({ ...prev, authEmail }));

  let listing: PortalListing | null;
  if (prefetched !== undefined) {
    listing = prefetched;
  } else {
    const res = await call<PortalListing[]>({
      type: 'lookup_listings',
      items: [{ source: ref.source, source_id: ref.sourceId }],
    });
    if (!res.ok) {
      if (res.detail === NOT_SIGNED_IN_DETAIL) {
        setState((prev) => ({ ...prev, phase: 'signed_out', authEmail: null }));
        return;
      }
      setState((prev) => ({ ...prev, phase: 'error', errorMessage: res.detail }));
      return;
    }
    listing = res.data[0] ?? null;
  }

  const saleApt =
    listing?.found
      ? listing.category_main === 'byt' && listing.category_type === 'prodej'
      : urlSaleApartmentHint(url);

  /* Show the panel for ANY listing we have (app link + facts). The only dead
   * end is a listing not in our DB whose URL clearly isn't a sale apartment —
   * nothing to link, no MF, no estimate. */
  const active = Boolean(listing?.found) || saleApt !== false;
  setState((prev) => ({
    ...prev, phase: active ? 'active' : 'deactivated',
    listing, isSaleApt: saleApt,
  }));
  if (!active) return;

  /* For a property we have, load the stage list so the in-pipeline control can
   * offer stage changes, and the collection list so the monitoring toggle can
   * pick a target (both non-blocking; cached across panel opens). */
  if (listing?.found && listing.property_id != null) {
    void loadStages();
    void loadCollections();
    void loadNotes();  // the property's existing notes (per-property, lazy)
  }

  /* Lazily load an existing estimation so the editable yield block appears
   * without blocking the MF headline (only the estimation section uses it). */
  const est = saleApt !== false ? listing?.latest_estimation : null;
  if (est != null) {
    const full = await call<EstimationRun>({
      type: 'get_estimation', run_id: est.estimation_id,
    });
    if (full.ok && full.data.status === 'success') {
      setState((prev) => seedFromRun(prev, full.data));
    }
  }
}

/* URL-only sale-apartment hint for the not-in-our-DB case. */
function urlSaleApartmentHint(url: string): boolean | null {
  const portal = portalForUrl(url);
  if (portal?.saleApartmentHint == null) return null;
  try {
    return portal.saleApartmentHint(new URL(url).pathname);
  } catch {
    return null;
  }
}

// ----------------------------------------------------------------------
// Entry: detail pages get the panel; other pages on a known portal host
// get the index-card overlay (a no-op if there are no listing cards).
// ----------------------------------------------------------------------

function main(): void {
  const url = window.location.href;
  const ref = detailRef(url);
  if (ref != null) {
    openPanel(ref, url).catch((err: unknown) => {
      console.error('[mf-ext] detail boot failed', err);
    });
    return;
  }
  if (portalForHost(window.location.hostname) != null) {
    runIndexOverlay(call, openPanel).catch((err: unknown) => {
      console.error('[mf-ext] index overlay failed', err);
    });
  }
}

main();
