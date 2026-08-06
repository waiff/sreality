/* The price-change ledger mark — an arrow + signed percentage, set on the
 * price's own baseline so `12 750 000 Kč ↓ 4,2 %` reads as one typographic
 * unit rather than a price with a badge stuck to it.
 *
 * POLARITY. A price CUT is favourable here and renders sage; a RISE is
 * unfavourable and renders brick. That inverts the stock-market convention on
 * purpose — this is a buyer's pipeline, and a seller dropping their ask is the
 * single most actionable event on the board. It is not a new decision: the
 * listing-detail `Stat` component has shipped exactly this mapping
 * (`pct > 0 → brick, pct < 0 → sage`) since the price-history block landed.
 *
 * FOUR STATES, NOT THREE. `properties.total_price_change_pct` is NULL when the
 * property has fewer than two observed prices, which is NOT the same as "the
 * price hasn't moved" — roughly 60% of pipeline cards are in that state today,
 * having been seen exactly once. Rendering a flat arrow for them would assert
 * stability we have not observed. So:
 *
 *   pct == null            → render NOTHING (absence is honest)
 *   pct === 0, changes 0   → flat arrow, muted: observed twice, never moved
 *   pct === 0, changes > 0 → flat arrow + "net 0" title: moved and came back
 *   pct < 0                → ↓ sage
 *   pct > 0                → ↑ brick
 *
 * `changes` (properties.price_change_count) is what separates the two flat
 * cases; `total_price_change_pct` alone cannot tell them apart, and a property
 * that went 10M → 8M → 10M is a very different prospect from one that never
 * moved.
 */

import { fmtPct } from '@/lib/format';

export interface PriceDeltaProps {
  /** properties.total_price_change_pct — signed (last − first) / first × 100. */
  pct: number | null | undefined;
  /** properties.price_change_count — consecutive-step changes, all time. */
  changes?: number | null;
  /** Muted rendering for a delisted row, matching the card's own recede. */
  muted?: boolean;
  className?: string;
}

type Tone = 'up' | 'down' | 'flat';

const TONE_VAR: Record<Tone, string> = {
  up: 'var(--color-brick)',
  down: 'var(--color-sage)',
  flat: 'var(--color-ink-4)',
};

/* 8px chevrons on a 10px box, stroke-only so they sit at the same visual
 * weight as the mono digits beside them. A filled triangle would out-shout the
 * price it annotates. */
function Arrow({ tone }: { tone: Tone }) {
  const d =
    tone === 'up'
      ? 'M5 8.5V1.5M5 1.5L2 4.5M5 1.5L8 4.5'
      : tone === 'down'
        ? 'M5 1.5V8.5M5 8.5L2 5.5M5 8.5L8 5.5'
        : 'M1.5 5H8.5';
  return (
    <svg
      viewBox="0 0 10 10"
      className="h-2.5 w-2.5 shrink-0"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d={d} />
    </svg>
  );
}

const changesNote = (changes: number | null | undefined): string => {
  if (changes == null || changes <= 0) return '';
  const noun = changes === 1 ? 'změna' : changes <= 4 ? 'změny' : 'změn';
  return ` · ${changes} ${noun} ceny`;
};

export default function PriceDelta({
  pct,
  changes,
  muted = false,
  className,
}: PriceDeltaProps) {
  // Fewer than two observed prices — we have no basis for a claim. Render
  // nothing rather than implying stability.
  if (pct == null || !Number.isFinite(pct)) return null;

  const tone: Tone = pct > 0 ? 'up' : pct < 0 ? 'down' : 'flat';
  const label =
    tone === 'flat'
      ? changes != null && changes > 0
        ? `Cena se vrátila na původní hodnotu${changesNote(changes)}`
        : 'Cena se od prvního záznamu nezměnila'
      : `${tone === 'down' ? 'Pokles' : 'Růst'} oproti prvně zaznamenané ceně: ${fmtPct(
          pct,
          { signed: true },
        )}${changesNote(changes)}`;

  return (
    <span
      title={label}
      aria-label={label}
      className={[
        'inline-flex shrink-0 items-center gap-0.5 font-mono tabular-nums',
        'text-[0.68rem] leading-none',
        className ?? '',
      ].join(' ')}
      style={{ color: muted ? 'var(--color-ink-4)' : TONE_VAR[tone] }}
    >
      <Arrow tone={tone} />
      {/* Magnitude only — the arrow already carries the sign, and "↓ −4,2 %"
          reads as a double negative. */}
      {fmtPct(Math.abs(pct))}
    </span>
  );
}
