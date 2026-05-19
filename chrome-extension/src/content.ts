/* sreality.cz/detail/* content script.
 *
 * Mounts a floating yield panel in a closed shadow root so sreality's
 * own CSS can't bleed in. The panel goes through three modes:
 *
 *   1. loading            — placeholder while we ask the API.
 *   2. no_run             — listing not estimated yet → "Run estimation".
 *   3. has_run            — yield panel, three editable fields, debounced
 *                           PATCH /estimations/:id/scenario on each edit.
 *
 * All network calls go through chrome.runtime.sendMessage to the
 * background worker — see background.ts and api.ts. */

import styles from './styles.css?inline';
import { extractSrealityId } from './sreality';
import type {
  ApiMessage,
  ApiResult,
  EstimationRun,
  YieldScenarioUpdate,
} from './types';

const DEFAULT_FOND_CZK_PER_M2 = 10;
const PATCH_DEBOUNCE_MS = 500;
const POLL_INTERVAL_MS = 2000;
const POLL_MAX_ATTEMPTS = 60;
const HOST_ELEMENT_ID = '__sreality_yield_panel_host__';

interface PanelState {
  mode: 'loading' | 'no_run' | 'has_run' | 'error';
  run: EstimationRun | null;
  /* Per-axis "touched" state — see SPA's YieldBlock. A null scenario
   * value with touched=false means "follow the default"; touched=true
   * means the operator owns this field. */
  rentTouched: boolean;
  costTouched: boolean;
  priceTouched: boolean;
  /* Live (possibly edited) values, ready to render. */
  rent: number | null;
  costPerM2: number | null;
  price: number | null;
  /* Surfaced to the operator when something fails. */
  errorMessage: string | null;
}

function call<T>(message: ApiMessage): Promise<ApiResult<T>> {
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

function defaultPrice(run: EstimationRun): number | null {
  /* Mirrors the SPA's YieldBlock: prefer the input price the
   * operator typed when creating the estimation, fall back to the
   * estimated sale price for sale-kind runs. The chrome extension
   * doesn't yet have access to the subject listing's price column,
   * so the "subject listing sale price" shortcut from the SPA is
   * skipped here. */
  if (run.input_purchase_price_czk != null) {
    return run.input_purchase_price_czk;
  }
  if (run.estimate_kind === 'sale' && run.estimated_sale_price_czk != null) {
    return run.estimated_sale_price_czk;
  }
  return null;
}

function defaultRent(run: EstimationRun): number | null {
  return run.estimated_monthly_rent_czk;
}

function initialState(run: EstimationRun): PanelState {
  const sc = run.scenario;
  return {
    mode: 'has_run',
    run,
    rentTouched: sc?.rent_czk != null,
    costTouched: sc?.fond_per_m2_czk != null,
    priceTouched: sc?.price_czk != null,
    rent: sc?.rent_czk ?? defaultRent(run),
    costPerM2: sc?.fond_per_m2_czk ?? DEFAULT_FOND_CZK_PER_M2,
    price: sc?.price_czk ?? defaultPrice(run),
    errorMessage: null,
  };
}

function formatCzk(n: number | null): string {
  if (n == null || !Number.isFinite(n)) return '—';
  return new Intl.NumberFormat('cs-CZ', {
    maximumFractionDigits: 0,
  }).format(Math.round(n));
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

function computeYield(state: PanelState): number | null {
  const { rent, costPerM2, price, run } = state;
  if (run == null) return null;
  const area = run.input_spec?.area_m2 ?? null;
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

/* Mounts the panel once and exposes a typed `render(state)` that
 * re-paints the body without re-creating the host or its shadow root.
 * Wiring up oninput / onclick lives in render — the panel is small
 * enough that a full re-render per state change is fine, except for
 * input elements that we keep focused while the operator types. */
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

  const render = (state: PanelState): void => {
    panel.innerHTML = '';

    const header = document.createElement('div');
    header.className = 'panel-header';
    const title = document.createElement('p');
    title.className = 'panel-title';
    title.textContent =
      state.mode === 'has_run' ? 'Yield' :
      state.mode === 'loading' ? 'Yield · loading' :
      state.mode === 'error' ? 'Yield · error' :
      'Yield · no estimation';
    header.appendChild(title);
    const close = document.createElement('button');
    close.className = 'close-btn';
    close.textContent = '×';
    close.title = 'Hide panel';
    close.onclick = () => host.remove();
    header.appendChild(close);
    panel.appendChild(header);

    const body = document.createElement('div');
    body.className = 'panel-body';
    panel.appendChild(body);

    if (state.mode === 'loading') {
      const p = document.createElement('p');
      p.style.color = 'var(--ink-4)';
      p.style.textAlign = 'center';
      p.style.padding = '0.5rem 0';
      p.textContent = 'Checking for existing estimation…';
      body.appendChild(p);
      return;
    }

    if (state.mode === 'no_run') {
      const empty = document.createElement('div');
      empty.className = 'empty-state';
      const p = document.createElement('p');
      p.textContent = 'No estimation for this listing yet.';
      empty.appendChild(p);
      const btn = document.createElement('button');
      btn.className = 'btn-primary';
      btn.textContent = 'Run estimation';
      btn.onclick = () => onCreateRun();
      empty.appendChild(btn);
      if (state.errorMessage != null) {
        const err = document.createElement('p');
        err.className = 'error';
        err.textContent = state.errorMessage;
        empty.appendChild(err);
      }
      body.appendChild(empty);
      return;
    }

    if (state.mode === 'error') {
      const p = document.createElement('p');
      p.className = 'error';
      p.textContent = state.errorMessage ?? 'Something went wrong.';
      body.appendChild(p);
      return;
    }

    /* has_run */
    const ydisp = document.createElement('div');
    ydisp.className = 'yield-display';
    const ylabel = document.createElement('p');
    ylabel.className = 'yield-label';
    ylabel.textContent = 'Gross yield';
    ydisp.appendChild(ylabel);
    const yval = document.createElement('p');
    const pct = computeYield(state);
    yval.className = 'yield-value' + (pct == null ? ' muted' : '');
    yval.textContent = pct != null ? `${pct.toFixed(2)} %` : '—';
    ydisp.appendChild(yval);
    body.appendChild(ydisp);

    const fields = document.createElement('div');
    fields.className = 'fields';
    fields.appendChild(buildField({
      key: 'rent',
      label: 'Monthly rent',
      suffix: 'Kč',
      value: state.rent,
      onInput: (v) => onEdit('rent', v),
    }));
    fields.appendChild(buildField({
      key: 'cost',
      label: 'Fond oprav + SVJ',
      suffix: 'Kč/m²',
      value: state.costPerM2,
      onInput: (v) => onEdit('cost', v),
    }));
    fields.appendChild(buildField({
      key: 'price',
      label: 'Listing price',
      suffix: 'Kč',
      value: state.price,
      onInput: (v) => onEdit('price', v),
    }));
    body.appendChild(fields);

    const actions = document.createElement('div');
    actions.className = 'actions';
    const status = document.createElement('span');
    const hasOverrides =
      state.rentTouched || state.costTouched || state.priceTouched;
    status.textContent = hasOverrides ? 'edited · synced' : 'live calculation';
    actions.appendChild(status);
    if (hasOverrides) {
      const reset = document.createElement('a');
      reset.textContent = 'Reset';
      reset.onclick = (e) => { e.preventDefault(); onReset(); };
      actions.appendChild(reset);
    }
    body.appendChild(actions);

    if (state.errorMessage != null) {
      const err = document.createElement('p');
      err.className = 'error';
      err.textContent = state.errorMessage;
      body.appendChild(err);
    }

    /* Restore focus to whichever input the operator was editing when
     * the last render fired. Without this, every keystroke would blur
     * the input because we wipe and re-create the elements. */
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

  return {
    shadow,
    render,
    destroy: () => host.remove(),
  };
}

/* App-level state machine. Boot, mount, kick off the lookup, react to
 * messages. Closed over by the inner callbacks in mountPanel.render. */
let state: PanelState;
let render: (s: PanelState) => void;
let patchTimer: ReturnType<typeof setTimeout> | null = null;

function setState(updater: (prev: PanelState) => PanelState): void {
  state = updater(state);
  render(state);
}

function schedulePatch(): void {
  if (patchTimer != null) clearTimeout(patchTimer);
  patchTimer = setTimeout(async () => {
    patchTimer = null;
    if (state.mode !== 'has_run' || state.run == null) return;
    const res = await call<EstimationRun>({
      type: 'patch_scenario',
      run_id: state.run.id,
      body: bodyFromState(state),
    });
    if (!res.ok) {
      setState((prev) => ({
        ...prev, errorMessage: `Save failed: ${res.detail}`,
      }));
      return;
    }
    setState((prev) => ({
      ...prev,
      run: res.data,
      errorMessage: null,
    }));
  }, PATCH_DEBOUNCE_MS);
}

function onEdit(axis: 'rent' | 'cost' | 'price', value: number | null): void {
  setState((prev) => {
    switch (axis) {
      case 'rent':
        return { ...prev, rent: value, rentTouched: true };
      case 'cost':
        return { ...prev, costPerM2: value, costTouched: true };
      case 'price':
        return { ...prev, price: value, priceTouched: true };
    }
  });
  schedulePatch();
}

function onReset(): void {
  if (state.run == null) return;
  setState((prev) => ({
    ...prev,
    rent: defaultRent(prev.run!),
    costPerM2: DEFAULT_FOND_CZK_PER_M2,
    price: defaultPrice(prev.run!),
    rentTouched: false,
    costTouched: false,
    priceTouched: false,
  }));
  schedulePatch();
}

async function onCreateRun(): Promise<void> {
  setState((prev) => ({ ...prev, mode: 'loading', errorMessage: null }));
  const res = await call<EstimationRun>({
    type: 'create_estimation',
    url: window.location.href,
  });
  if (!res.ok) {
    setState((prev) => ({
      ...prev,
      mode: 'no_run',
      errorMessage: `Couldn't start estimation: ${res.detail}`,
    }));
    return;
  }
  /* The POST may return a still-pending row when the backend has
   * scheduled the heavy work as a BackgroundTask. Poll until terminal. */
  let row = res.data;
  for (let i = 0; i < POLL_MAX_ATTEMPTS; i++) {
    if (row.status === 'success' || row.status === 'failed') break;
    await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
    const next = await call<EstimationRun>({
      type: 'get_estimation', run_id: row.id,
    });
    if (!next.ok) break;
    row = next.data;
  }
  if (row.status !== 'success') {
    setState((prev) => ({
      ...prev,
      mode: 'no_run',
      errorMessage:
        row.error_message ?? 'Estimation did not complete in time.',
    }));
    return;
  }
  state = initialState(row);
  render(state);
}

async function boot(): Promise<void> {
  const id = extractSrealityId(window.location.href);
  if (id == null) return;

  const panel = mountPanel();
  render = panel.render;
  state = {
    mode: 'loading', run: null,
    rentTouched: false, costTouched: false, priceTouched: false,
    rent: null, costPerM2: null, price: null,
    errorMessage: null,
  };
  render(state);

  const res = await call<EstimationRun | null>({
    type: 'find_run_by_sreality_id', sreality_id: id,
  });

  if (!res.ok) {
    setState((prev) => ({
      ...prev, mode: 'error', errorMessage: res.detail,
    }));
    return;
  }

  if (res.data == null) {
    setState((prev) => ({ ...prev, mode: 'no_run' }));
    return;
  }

  state = initialState(res.data);
  render(state);
}

boot().catch((err: unknown) => {
  /* Last-ditch — the panel should never throw the page. Log so a
   * developer can inspect via the page console. */
  console.error('[sreality-ext] boot failed', err);
});
