/* Shared chrome for the two admin "settings registry" pages (Settings,
 * NewDedupSettings): a section wrapper with a coherent expand/collapse
 * twisty + a ledger-style folio number, and the compact-description
 * mechanism (an info icon that reveals its text on hover, with a
 * page-level toggle to switch every description back to always-inline —
 * "the info as it is now" for reviewing many settings at once).
 *
 * One rule threads both pages: a description never *only* lives inside a
 * collapsed section. The hint icon sits in the header, so it's readable by
 * hover even while the body is folded; the full paragraph only replaces it
 * when the body is open AND the page is in Detailed mode.
 */

import { useState, type ReactNode } from 'react';
import { InfoIcon } from '@/components/icons';
import { PickButton } from '@/components/controls';

/* -------------------------------------------------------------------- */
/* Expand/collapse twisty — right-pointing when closed, rotates to point  */
/* down when open. The one glyph used for every collapsible header and    */
/* card row on both settings pages.                                       */
/* -------------------------------------------------------------------- */

export function Chevron({ open, className = '' }: { open: boolean; className?: string }) {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 16 16"
      aria-hidden="true"
      className={[
        'shrink-0 text-[var(--color-ink-4)] transition-transform',
        open ? 'rotate-90' : '',
        className,
      ].join(' ')}
    >
      <path
        d="M6 4l4 4-4 4"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/* -------------------------------------------------------------------- */
/* Info hint — the compact form of a description: an (i) glyph that shows */
/* its text natively on hover/focus, the same "cursor-help" idiom already  */
/* used across the app (Pipeline's board-card Hint, ConfidenceIndicator).  */
/* -------------------------------------------------------------------- */

export function InfoHint({ text, className = '' }: { text: string; className?: string }) {
  return (
    <span
      role="img"
      aria-label={text}
      title={text}
      className={[
        'inline-flex shrink-0 items-center justify-center cursor-help text-[var(--color-ink-3)] hover:text-[var(--color-copper)] transition-colors',
        className,
      ].join(' ')}
    >
      <InfoIcon className="h-4 w-4" />
    </span>
  );
}

/* -------------------------------------------------------------------- */
/* Info mode — page-scoped, localStorage-persisted. false = Compact       */
/* (descriptions collapse to InfoHint icons); true = Detailed (every      */
/* description renders inline, the pre-existing behaviour, for scanning   */
/* many settings at once without hovering each one).                      */
/* -------------------------------------------------------------------- */

export function useInfoMode(pageKey: string): [boolean, (next: boolean) => void] {
  const key = `settings.info-mode.${pageKey}`;
  const [expanded, setExpandedState] = useState<boolean>(() => {
    try {
      return localStorage.getItem(key) === 'detailed';
    } catch {
      return false;
    }
  });
  const setExpanded = (next: boolean) => {
    setExpandedState(next);
    try {
      localStorage.setItem(key, next ? 'detailed' : 'compact');
    } catch {
      /* storage may be unavailable — toggle still works in-session */
    }
  };
  return [expanded, setExpanded];
}

export function InfoModeToggle({
  expanded,
  onChange,
}: {
  expanded: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <div role="group" aria-label="Description density" className="inline-flex items-center gap-2 shrink-0">
      <span className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
        Descriptions
      </span>
      <div className="inline-flex gap-1">
        <PickButton on={!expanded} onClick={() => onChange(false)}>
          Compact
        </PickButton>
        <PickButton on={expanded} onClick={() => onChange(true)}>
          Detailed
        </PickButton>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* CollapsibleSection — a clickable header (chevron + folio number +      */
/* eyebrow + display-serif title) with the body hidden when collapsed.    */
/* Per-section open/closed state persists in localStorage (the pages are  */
/* long) via the same `settings.collapsed.<id>` scheme both pages share.  */
/* -------------------------------------------------------------------- */

export function useCollapsed(id: string, defaultOpen: boolean): [boolean, () => void] {
  const key = `settings.collapsed.${id}`;
  const [open, setOpen] = useState<boolean>(() => {
    try {
      const v = localStorage.getItem(key);
      return v == null ? defaultOpen : v === 'open';
    } catch {
      return defaultOpen;
    }
  });
  const toggle = () =>
    setOpen((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(key, next ? 'open' : 'closed');
      } catch {
        /* storage may be unavailable — collapse still works in-session */
      }
      return next;
    });
  return [open, toggle];
}

export function CollapsibleSection({
  id,
  index,
  title,
  eyebrow,
  hint,
  description,
  infoExpanded,
  defaultOpen = true,
  children,
}: {
  id: string;
  title: string;
  eyebrow?: string;
  /* Ledger-style folio number ("01", "02", …) — pass for a page's own
   * top-level sections; omit where the title already carries its own
   * ordinal (NEW DEDUP's "L0 · …" category labels), so the header never
   * shows two competing numbering systems. */
  index?: number;
  /* Plain-text twin of `description`, for the InfoHint tooltip. Only
   * needed when `description` is rich (JSX) — a string description
   * doubles as its own hint. */
  hint?: string;
  description?: ReactNode;
  infoExpanded: boolean;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const [isOpen, toggle] = useCollapsed(id, defaultOpen);
  const hintText = hint ?? (typeof description === 'string' ? description : undefined);
  return (
    <section
      id={id}
      className="pt-8 border-t border-[var(--color-rule-strong)] first-of-type:pt-0 first-of-type:border-t-0 scroll-mt-20"
    >
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={toggle}
          aria-expanded={isOpen}
          className="group flex flex-1 min-w-0 items-center gap-2.5 text-left"
        >
          <Chevron open={isOpen} />
          <span className="min-w-0">
            {(index != null || eyebrow) && (
              <span className="flex items-baseline gap-1.5">
                {index != null && (
                  <span className="font-mono text-[0.68rem] tabular-nums text-[var(--color-copper)]">
                    {String(index).padStart(2, '0')}
                  </span>
                )}
                {eyebrow && (
                  <span className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
                    {eyebrow}
                  </span>
                )}
              </span>
            )}
            <span
              className="block text-xl group-hover:text-[var(--color-copper-2)] transition-colors"
              style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
            >
              {title}
            </span>
          </span>
        </button>
        {hintText && !infoExpanded && <InfoHint text={hintText} className="mt-0.5" />}
      </div>
      {isOpen ? (
        <div className="mt-3">
          {infoExpanded && description ? (
            <p className="text-sm text-[var(--color-ink-3)] mb-3 leading-relaxed max-w-[52rem]">
              {description}
            </p>
          ) : null}
          {children}
        </div>
      ) : null}
    </section>
  );
}

/* -------------------------------------------------------------------- */
/* Shared error banner                                                   */
/* -------------------------------------------------------------------- */

export function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="p-3 rounded-[var(--radius-sm)] border border-[var(--color-brick)]/30 bg-[var(--color-brick-soft)] text-sm text-[var(--color-brick)]">
      <strong className="font-medium">Failed:</strong> {message}
    </div>
  );
}
