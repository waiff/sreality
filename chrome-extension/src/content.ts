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
  EstimationRun,
  PipelineCardResult,
  PortalListing,
  YieldScenarioUpdate,
} from './types';

const DEFAULT_FOND_CZK_PER_M2 = 10;
const PATCH_DEBOUNCE_MS = 500;
const POLL_INTERVAL_MS = 2000;
const POLL_MAX_ATTEMPTS = 60;
const HOST_ELEMENT_ID = '__sreality_yield_panel_host__';

/* SPA base URL for the "Otevřít v aplikaci" deep-link, inlined at build time.
 * Inlined here (not shared with api.ts) because MV3 content scripts are classic
 * scripts that can't `import` — content.js must stay self-contained. Empty →
 * link hidden. Default https when the operator omits the scheme. */
const APP_BASE_URL = ((raw: string): string => {
  const t = raw.trim();
  if (t === '') return '';
  return (/^https?:\/\//i.test(t) ? t : `https://${t}`).replace(/\/$/, '');
})(import.meta.env.VITE_APP_BASE_URL ?? '');

type Phase = 'loading' | 'deactivated' | 'active' | 'error';

interface PanelState {
  phase: Phase;
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
  rent: number | null;
  costPerM2: number | null;
  price: number | null;
  /* True while an estimation is being created/polled. */
  busy: boolean;
  /* True while a pipeline add/remove is in flight (disables the toggle). */
  pipelineBusy: boolean;
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

/* The SPA's <FilterIcon> (icons.tsx) hand-reproduced as inline SVG — the shared
 * "pipeline" glyph on every surface (filled knobs = in-pipeline). The extension
 * can't import the SPA's React component (separate territory, classic content
 * script), so this mirrors it by value, like the palette in styles.css. */
function filterIconSvg(filled: boolean): string {
  const f = filled ? 'currentColor' : 'none';
  return (
    '<svg class="pipeline-icon" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="1.75" stroke-linecap="round" ' +
    'stroke-linejoin="round" aria-hidden="true">' +
    '<line x1="3" y1="7" x2="12.7" y2="7"/><line x1="17.3" y1="7" x2="21" y2="7"/>' +
    '<line x1="3" y1="12" x2="6.7" y2="12"/><line x1="11.3" y1="12" x2="21" y2="12"/>' +
    '<line x1="3" y1="17" x2="13.7" y2="17"/><line x1="18.3" y1="17" x2="21" y2="17"/>' +
    `<circle cx="15" cy="7" r="2.4" fill="${f}"/>` +
    `<circle cx="9" cy="12" r="2.4" fill="${f}"/>` +
    `<circle cx="16" cy="17" r="2.4" fill="${f}"/></svg>`
  );
}

/* Immutably set the listing's pipeline membership on a state update. */
function withPipeline(
  prev: PanelState, membership: PortalListing['pipeline'],
): PanelState {
  if (prev.listing == null) return prev;
  return { ...prev, listing: { ...prev.listing, pipeline: membership } };
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
  const { rent, costPerM2, price } = state;
  const area = subjectArea(state);
  const fond = costPerM2 != null && area != null ? costPerM2 * area : null;
  if (rent == null || fond == null || price == null || price <= 0) return null;
  return ((rent - fond) * 12) / price * 100;
}

function bodyFromState(state: PanelState): YieldScenarioUpdate {
  return {
    rent_czk: state.rentTouched ? state.rent : null,
    fond_per_m2_czk: state.costTouched ? state.costPerM2 : null,
    price_czk: state.priceTouched ? state.price : null,
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
    rent: sc?.rent_czk ?? defaultRent(next),
    costPerM2: sc?.fond_per_m2_czk ?? DEFAULT_FOND_CZK_PER_M2,
    price: sc?.price_czk ?? defaultPrice(next),
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

  let lastFocusedKey: 'rent' | 'cost' | 'price' | null = null;

  function header(text: string): HTMLElement {
    const h = document.createElement('div');
    h.className = 'panel-header';
    const title = document.createElement('p');
    title.className = 'panel-title';
    title.textContent = text;
    h.appendChild(title);
    const close = document.createElement('button');
    close.className = 'close-btn';
    close.textContent = '×';
    close.title = 'Skrýt panel';
    close.onclick = () => host.remove();
    h.appendChild(close);
    return h;
  }

  const render = (state: PanelState): void => {
    panel.innerHTML = '';
    panel.classList.toggle('panel--muted', state.phase === 'deactivated');

    panel.appendChild(header('Výnos MF'));
    const body = document.createElement('div');
    body.className = 'panel-body';
    panel.appendChild(body);

    if (state.phase === 'loading') {
      body.appendChild(note('Načítám data…'));
      return;
    }
    if (state.phase === 'error') {
      body.appendChild(errorLine(state.errorMessage ?? 'Něco se nepovedlo.'));
      return;
    }
    if (state.phase === 'deactivated') {
      body.appendChild(note('Tato nemovitost není v naší databázi.'));
      return;
    }

    /* active — the pipeline bookmark + "open in our app" link + subject facts
     * show for ANY listing we have; the MF headline + estimation are gated to
     * apartments for sale. */
    renderTopActions(body, state);
    if (state.isSaleApt !== false) {
      renderMfBlock(body, state);
      renderSubjectFacts(body, state);
      renderEstimation(body, state);
    } else {
      renderSubjectFacts(body, state);
      body.appendChild(note('Výnos MF a odhad jsou jen u bytů na prodej.'));
    }
    if (state.errorMessage != null) body.appendChild(errorLine(state.errorMessage));

    /* Restore focus after a full re-render so typing isn't interrupted. */
    if (lastFocusedKey != null) {
      const target = shadow.querySelector<HTMLInputElement>(
        `input[data-key="${lastFocusedKey}"]`,
      );
      if (target != null) {
        target.focus();
        const v = target.value;
        try { target.setSelectionRange(v.length, v.length); } catch { /* */ }
      }
    }
  };

  function note(text: string): HTMLElement {
    const p = document.createElement('p');
    p.className = 'muted-note';
    p.textContent = text;
    return p;
  }

  function errorLine(text: string): HTMLElement {
    const p = document.createElement('p');
    p.className = 'error';
    p.textContent = text;
    return p;
  }

  /* The two "this listing ↔ our app" affordances in one row: the deal-pipeline
   * bookmark (left) + the "open in our app" deep-link (right). Either may be
   * absent (no property yet / no SPA base configured); the row appears only if
   * something landed in it. */
  function renderTopActions(body: HTMLElement, state: PanelState): void {
    const row = document.createElement('div');
    row.className = 'top-actions';
    renderPipelineToggle(row, state);
    renderAppLink(row, state);
    if (row.childElementCount > 0) body.appendChild(row);
  }

  function renderAppLink(container: HTMLElement, state: PanelState): void {
    const sid = state.listing?.sreality_id;
    if (sid == null || !APP_BASE_URL) return;  // not in our DB, or base unset
    const a = document.createElement('a');
    a.className = 'app-link';
    a.href = `${APP_BASE_URL}/listing/${sid}`;  // same template as every SPA surface
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = 'Otevřít v aplikaci →';
    container.appendChild(a);
  }

  /* Deal-pipeline bookmark for the listing's property (rule #22). Same contract
   * as the SPA's PipelineToggle / Browse-card ★: out of pipeline → a copper
   * "Přidat do pipeline"; in pipeline → a filled pill showing the stage label;
   * click toggles add/remove. Property-grain — shown for ANY listing we have a
   * property for (not gated to sale apartments), hidden during the brief window
   * a freshly-scraped row has no property_id yet. */
  function renderPipelineToggle(container: HTMLElement, state: PanelState): void {
    const l = state.listing;
    if (l == null || !l.found || l.property_id == null) return;
    const inPipe = l.pipeline?.in_pipeline ?? false;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'pipeline-toggle' + (inPipe ? ' pipeline-toggle--in' : '');
    btn.disabled = state.pipelineBusy;
    btn.setAttribute('aria-pressed', String(inPipe));
    btn.title = inPipe ? 'Odebrat z pipeline' : 'Přidat do pipeline';
    btn.innerHTML = filterIconSvg(inPipe);
    const label = document.createElement('span');
    label.textContent = inPipe
      ? (l.pipeline?.stage_label ?? 'V pipeline')
      : 'Přidat do pipeline';
    btn.appendChild(label);
    btn.onclick = () => { void onTogglePipeline(); };
    container.appendChild(btn);
  }

  function renderMfBlock(body: HTMLElement, state: PanelState): void {
    const l = state.listing;
    const ydisp = document.createElement('div');
    ydisp.className = 'yield-display';
    const ylabel = document.createElement('p');
    ylabel.className = 'yield-label';
    ylabel.textContent = 'Výnos MF (hrubý)';
    ylabel.title = 'Hrubý výnos dle cenové mapy nájemného MF (nájem ÷ cena)';
    ydisp.appendChild(ylabel);
    const yval = document.createElement('p');
    const pct = l?.mf_gross_yield_pct ?? null;
    yval.className = 'yield-value' + (pct == null ? ' muted' : '');
    yval.textContent = fmtPct(pct);
    ydisp.appendChild(yval);
    body.appendChild(ydisp);

    if (l?.mf_reference_rent_czk != null) {
      const area = l.area_m2 ?? null;
      const perM2 = area && area > 0 ? Math.round(l.mf_reference_rent_czk / area) : null;
      const row = document.createElement('div');
      row.className = 'ref-rent';
      const rl = document.createElement('span');
      rl.className = 'ref-rent-label';
      rl.textContent = 'MF nájem';
      const rv = document.createElement('span');
      rv.className = 'ref-rent-value';
      rv.textContent =
        `${fmtCzk(l.mf_reference_rent_czk)}/měs` +
        (perM2 != null ? ` · ${perM2.toLocaleString('cs-CZ')} Kč/m²` : '');
      row.appendChild(rl);
      row.appendChild(rv);
      body.appendChild(row);
    } else if (l?.found) {
      body.appendChild(note('MF nájem nedostupný (chybí území nebo cena).'));
    } else {
      body.appendChild(note('Tato nemovitost není v naší databázi.'));
    }
  }

  function renderSubjectFacts(body: HTMLElement, state: PanelState): void {
    const l = state.listing;
    if (l == null || !l.found) return;
    const parts: string[] = [];
    if (l.disposition) parts.push(l.disposition);
    if (l.area_m2 != null) parts.push(`${Math.round(l.area_m2)} m²`);
    if (l.price_czk != null) parts.push(fmtCzk(l.price_czk));
    if (l.district || l.locality) parts.push((l.district ?? l.locality) as string);
    if (parts.length === 0) return;
    const p = document.createElement('p');
    p.className = 'subject-facts';
    p.textContent = parts.join(' · ');
    body.appendChild(p);
  }

  function renderEstimation(body: HTMLElement, state: PanelState): void {
    const sec = document.createElement('div');
    sec.className = 'est-section';
    const head = document.createElement('p');
    head.className = 'section-label';
    head.textContent = 'Odhad výnosu (komparativní)';
    sec.appendChild(head);

    /* No estimation yet (or one is running). */
    if (state.run == null) {
      if (state.busy) {
        sec.appendChild(note('Odhad probíhá… (~10–30 s)'));
      } else {
        const btn = document.createElement('button');
        btn.className = 'btn-primary';
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
    }));
    fields.appendChild(buildField({
      key: 'price', label: 'Cena', suffix: 'Kč',
      value: state.price, onInput: (v) => onEdit('price', v),
    }));
    sec.appendChild(fields);

    const yieldRow = document.createElement('div');
    yieldRow.className = 'est-yield';
    const yl = document.createElement('span');
    yl.textContent = 'Výnos z odhadu';
    const yv = document.createElement('span');
    yv.className = 'est-yield-value';
    yv.textContent = fmtPct(computeYield(state));
    yieldRow.appendChild(yl);
    yieldRow.appendChild(yv);
    sec.appendChild(yieldRow);

    const actions = document.createElement('div');
    actions.className = 'actions';
    const status = document.createElement('span');
    status.className = 'est-status';
    const hasOverrides = state.rentTouched || state.costTouched || state.priceTouched;
    status.textContent = hasOverrides ? 'upraveno · uloženo' : 'živý výpočet';
    actions.appendChild(status);
    /* Reset is always present (toggled), so onEdit can reveal it in place
     * without a full re-render — see onEdit / overridesChanged. */
    const reset = document.createElement('a');
    reset.className = 'est-reset';
    reset.textContent = 'Reset';
    reset.style.display = hasOverrides ? '' : 'none';
    reset.onclick = (e) => { e.preventDefault(); onReset(); };
    actions.appendChild(reset);
    sec.appendChild(actions);
    body.appendChild(sec);
  }

  function buildField(opts: {
    key: 'rent' | 'cost' | 'price';
    label: string;
    suffix: string;
    value: number | null;
    onInput: (v: number | null) => void;
  }): HTMLElement {
    const wrap = document.createElement('div');
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
      setState((prev) => ({ ...prev, errorMessage: `Uložení selhalo: ${res.detail}` }));
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
function onEdit(axis: 'rent' | 'cost' | 'price', value: number | null): void {
  switch (axis) {
    case 'rent': state.rent = value; state.rentTouched = true; break;
    case 'cost': state.costPerM2 = value; state.costTouched = true; break;
    case 'price': state.price = value; state.priceTouched = true; break;
  }
  const yv = panelShadow?.querySelector<HTMLElement>('.est-yield-value');
  if (yv != null) yv.textContent = fmtPct(computeYield(state));
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
    rentTouched: false, costTouched: false, priceTouched: false,
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
      ...prev, busy: false, errorMessage: `Odhad se nepodařilo spustit: ${res.detail}`,
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
async function onTogglePipeline(): Promise<void> {
  const l = state.listing;
  if (l == null || l.property_id == null) return;
  const propertyId = l.property_id;
  const wasIn = l.pipeline?.in_pipeline ?? false;
  const priorStageKey = l.pipeline?.stage_key ?? null;
  const priorStageLabel = l.pipeline?.stage_label ?? null;

  /* Apply a membership update only if the panel STILL represents the property
   * the toggle was started for. The panel state is a single module global that
   * openPanel() replaces wholesale — re-opening for a different card (an index
   * badge, a MutationObserver re-pass) mid-request would otherwise bleed this
   * listing's result onto that one. Network writes target the captured
   * propertyId regardless; this guard only protects the display. */
  const applyIfSame = (
    membership: PortalListing['pipeline'], patch: Partial<PanelState>,
  ) => (prev: PanelState): PanelState =>
    prev.listing?.property_id === propertyId
      ? withPipeline({ ...prev, ...patch }, membership)
      : prev;

  setState(applyIfSame(
    { in_pipeline: !wasIn, stage_key: null, stage_label: null },
    { pipelineBusy: true, errorMessage: null },
  ));

  const res = await call<PipelineCardResult>({
    type: wasIn ? 'remove_pipeline_card' : 'add_pipeline_card',
    property_id: propertyId,
  });

  if (!res.ok) {
    setState(applyIfSame(
      { in_pipeline: wasIn, stage_key: priorStageKey, stage_label: priorStageLabel },
      { pipelineBusy: false, errorMessage: `Uložení do pipeline selhalo: ${res.detail}` },
    ));
    return;
  }

  setState(applyIfSame(
    wasIn
      ? { in_pipeline: false, stage_key: null, stage_label: null }
      : {
          in_pipeline: true,
          stage_key: res.data.stage_key ?? null,
          stage_label: res.data.stage_label ?? null,
        },
    { pipelineBusy: false },
  ));
}

/* Mounts/refreshes the floating panel for one listing. Used by the detail-page
 * entry AND by index-card badges (which pass the card's ref + href + the
 * already-fetched listing so no second lookup is needed). */
export async function openPanel(
  ref: PortalRef, url: string, prefetched?: PortalListing | null,
): Promise<void> {
  panelUrl = url;
  const panel = mountPanel();
  render = panel.render;
  panelShadow = panel.shadow;
  state = {
    phase: 'loading', listing: null, isSaleApt: null, run: null,
    rentTouched: false, costTouched: false, priceTouched: false,
    rent: null, costPerM2: null, price: null, busy: false,
    pipelineBusy: false, errorMessage: null,
  };
  render(state);

  let listing: PortalListing | null;
  if (prefetched !== undefined) {
    listing = prefetched;
  } else {
    const res = await call<PortalListing[]>({
      type: 'lookup_listings',
      items: [{ source: ref.source, source_id: ref.sourceId }],
    });
    if (!res.ok) {
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
