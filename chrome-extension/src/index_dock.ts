/* The search page's corner dock — the one page-level thing the extension shows
 * on EVERY index page with listing cards, whatever their category: a small bar
 * in the bottom-left corner (the panel owns the bottom-right) holding the
 * "Skrýt skryté" switch. Above it, in the same column so nothing overlaps: a
 * slot for the lookup-failure notice (index_overlay.ts) and a line for the
 * reason a card write failed.
 *
 * The switch decides how a property the operator DISMISSED ("Skrýt", migration
 * 536) is drawn among the portal's own results: on — its card stays in place
 * but barely visible; off — it is drawn like any other card, told apart only
 * by its filled eye. The card is never removed: pulling nodes out of a portal's
 * result grid breaks its layout and its paging, and a card that is gone can't
 * be un-hidden from where it stood. The preference is per browser, kept in
 * chrome.storage.local like the panel's minimized state, and follows the
 * operator across portals and tabs. */

import tokens from './tokens.css?inline';

const DOCK_HOST_ID = '__mf_dock_host__';
const VEIL_KEY = 'veilDismissed';
/* A failed write's reason stays up this long, then clears itself. */
const FLASH_MS = 8_000;

/* On unless the operator turned it off: a dismissal means "never show me this
 * again", so the default honours it. */
export async function readVeil(): Promise<boolean> {
  try {
    const stored = await chrome.storage.local.get([VEIL_KEY]);
    return stored[VEIL_KEY] !== false;
  } catch {
    return true;  // storage unavailable (an orphaned script) → the default
  }
}

export function writeVeil(on: boolean): void {
  try {
    void chrome.storage.local.set({ [VEIL_KEY]: on }).catch(() => {});
  } catch {
    /* orphaned script — the switch still works for this page */
  }
}

/* The switch flipped in another tab (or on another portal). Returns the unsubscribe. */
export function onVeilChange(fn: (on: boolean) => void): () => void {
  const listener = (
    changes: { [key: string]: chrome.storage.StorageChange }, area: string,
  ): void => {
    if (area === 'local' && VEIL_KEY in changes) fn(changes[VEIL_KEY].newValue !== false);
  };
  try {
    chrome.storage.onChanged.addListener(listener);
  } catch {
    return () => {};
  }
  return () => {
    try { chrome.storage.onChanged.removeListener(listener); } catch { /* orphaned */ }
  };
}

const DOCK_CSS = `
  ${tokens}
  :host { all: initial; }
  [hidden] { display: none !important; }
  .dock {
    position: fixed; left: 20px; bottom: 20px; z-index: 2147483646;
    display: flex; flex-direction: column; align-items: flex-start; gap: 8px;
    max-width: calc(100vw - 40px);
    font-family: 'Inter', system-ui, -apple-system, sans-serif;
    pointer-events: none;
  }
  .dock > * { pointer-events: auto; }
  .slot:empty { display: none; }
  .flash {
    box-sizing: border-box; margin: 0; padding: 6px 10px; max-width: 336px;
    font-size: 12px; line-height: 1.4; overflow-wrap: anywhere;
    color: #b34730; background: var(--bg); border: 1px solid #b34730;
    box-shadow: 0 6px 20px -8px rgba(28, 20, 10, 0.30);
  }
  .bar {
    box-sizing: border-box; display: inline-flex; align-items: center; gap: 8px;
    height: 28px; padding: 0 10px 0 8px;
    font-size: 12px; line-height: 1; color: var(--ink);
    background: var(--bg); border: 1px solid var(--rule-strong);
    box-shadow: 0 6px 20px -8px rgba(28, 20, 10, 0.30);
  }
  .tick { width: 8px; height: 8px; background: var(--copper); flex-shrink: 0; }
  .switch {
    display: inline-flex; align-items: center; gap: 8px; margin: 0; padding: 0;
    font: inherit; color: inherit; background: transparent; border: 0; cursor: pointer;
  }
  .switch:focus-visible { outline: 2px solid var(--copper); outline-offset: 3px; }
  .track {
    box-sizing: border-box; position: relative; width: 26px; height: 14px; flex-shrink: 0;
    background: var(--paper); border: 1px solid var(--ink-4);
    transition: background 120ms ease, border-color 120ms ease;
  }
  .thumb {
    position: absolute; top: 1px; left: 1px; width: 10px; height: 10px;
    background: var(--ink-4);
    transition: transform 120ms ease, background 120ms ease;
  }
  .switch[aria-checked="true"] .track { background: var(--copper-soft); border-color: var(--copper); }
  .switch[aria-checked="true"] .thumb { background: var(--copper); transform: translateX(12px); }
  .count {
    padding-left: 8px; color: var(--ink-3); border-left: 1px solid var(--rule);
    font-variant-numeric: tabular-nums; white-space: nowrap;
  }
  @media (prefers-reduced-motion: reduce) { .track, .thumb { transition: none; } }
`;

export interface DockHandle {
  /* Where the lookup-failure notice mounts (above the bar, same column). */
  slot: HTMLElement;
  setVeil: (on: boolean) => void;
  /* How many cards on this page belong to dismissed properties. */
  setCount: (n: number) => void;
  /* Say why a card write failed; clears itself. */
  flash: (message: string) => void;
  connected: () => boolean;
  destroy: () => void;
}

/* `extraCss` styles whatever the caller mounts into `slot` — the dock's closed
 * shadow root is the only stylesheet scope that reaches it. */
export function mountDock(extraCss: string, onToggle: () => void): DockHandle {
  document.getElementById(DOCK_HOST_ID)?.remove();
  const host = document.createElement('div');
  host.id = DOCK_HOST_ID;
  const shadow = host.attachShadow({ mode: 'closed' });
  const style = document.createElement('style');
  style.textContent = DOCK_CSS + extraCss;
  shadow.appendChild(style);

  const dock = document.createElement('div');
  dock.className = 'dock';
  const slot = document.createElement('div');
  slot.className = 'slot';
  const flashLine = document.createElement('p');
  flashLine.className = 'flash';
  flashLine.setAttribute('role', 'alert');
  flashLine.hidden = true;

  const bar = document.createElement('div');
  bar.className = 'bar';
  const tick = document.createElement('span');
  tick.className = 'tick';
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'switch';
  toggle.setAttribute('role', 'switch');
  const label = document.createElement('span');
  label.textContent = 'Skrýt skryté';
  const track = document.createElement('span');
  track.className = 'track';
  track.setAttribute('aria-hidden', 'true');
  const thumb = document.createElement('span');
  thumb.className = 'thumb';
  track.appendChild(thumb);
  toggle.append(label, track);
  toggle.onclick = onToggle;
  const count = document.createElement('span');
  count.className = 'count';
  count.hidden = true;
  bar.append(tick, toggle, count);

  dock.append(slot, flashLine, bar);
  shadow.appendChild(dock);
  document.body.appendChild(host);

  let flashTimer: ReturnType<typeof setTimeout> | null = null;

  return {
    slot,
    setVeil(on: boolean): void {
      toggle.setAttribute('aria-checked', String(on));
      toggle.title = on
        ? 'Skryté nemovitosti jsou na stránce sotva vidět — kliknutím je zobrazíte normálně'
        : 'Skryté nemovitosti se zobrazují normálně — kliknutím je ztlumíte';
    },
    setCount(n: number): void {
      const text = n > 0 ? `${n} na stránce` : '';
      if (count.textContent !== text) count.textContent = text;
      count.hidden = n === 0;
    },
    flash(message: string): void {
      if (flashTimer != null) clearTimeout(flashTimer);
      flashLine.textContent = message;
      flashLine.hidden = false;
      flashTimer = setTimeout(() => {
        flashTimer = null;
        flashLine.hidden = true;
        flashLine.textContent = '';
      }, FLASH_MS);
    },
    connected: () => host.isConnected,
    destroy(): void {
      if (flashTimer != null) clearTimeout(flashTimer);
      host.remove();
    },
  };
}
