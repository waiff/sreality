/* "Skip to content" — the first tab stop on every page (WCAG 2.4.1 Bypass
 * Blocks). Without it a keyboard user walks ~15–20 identical chrome tab stops
 * (nav, menus, account) before reaching content, on EVERY navigation.
 *
 * Visually hidden until focused, then shown as a small pill at the top-left,
 * so sighted keyboard users see where they are and mouse users never see it.
 * Targets the <main id="main" tabIndex={-1}> landmark in Shell; the hash
 * navigation moves focus there natively, no JS needed. */
import { MAIN_ID } from '@/lib/useRouteFocus';

export default function SkipLink() {
  return (
    <a
      href={`#${MAIN_ID}`}
      className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50 focus:px-3 focus:py-1.5 focus:rounded-[var(--radius-sm)] focus:bg-[var(--color-paper-3)] focus:text-[var(--color-ink)] focus:border focus:border-[var(--color-rule-strong)] focus:text-sm"
    >
      Skip to content
    </a>
  );
}
