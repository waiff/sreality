/* The civic index strip — four municipal quality readings along a card's
 * bottom edge.
 *
 * FORM. Not four pills. A fixed four-cell strip, each cell captioned with a
 * two-letter abbreviation and underscored by a 2px rule in its band colour.
 * Two reasons it is a strip and not a badge set:
 *
 *   - POSITIONAL READABILITY. The four indexes are always the same four in the
 *     same order, so the third cell is *always* young-migration. An operator
 *     scanning thirty cards learns the positions and reads the strip
 *     pre-attentively, without reading a label. A variable-length pill set
 *     cannot be read that way.
 *   - IT REPEATS A MOTIF ALREADY ON THE PAGE. The kanban's column headers are
 *     separated by a `border-b-2` in the stage colour; the strip is that same
 *     coloured rule at card scale — a registry folder's coloured tab edge.
 *
 * COLOUR CARRIES GIST, THE NUMBER CARRIES PRECISION. Three bands, not a
 * continuous ramp: measured on this surface, adjacent steps of a low-alpha
 * continuous tint separate by ΔE 3.5–4.9, i.e. indistinguishable even with
 * full colour vision. Three well-separated bands measure ΔE 21.9 (light) /
 * 16.9 (dark). The exact value is printed in the cell, so nothing is lost —
 * and per the accessibility rule for sub-3:1 fills, that printed value IS the
 * required relief. Colour is never the only channel.
 *
 * The band scale itself lives in lib/cityIndexScale, shared with the map.
 */

import {
  bandForValue,
  INDEX_BAND_VAR,
  type IndexBand,
} from '@/lib/cityIndexScale';
import { CARD_INDEX_ABBR, indexLabel } from '@/lib/cityIndexes';
import type { CityQuality } from '@/lib/useCityQuality';

const BAND_WORD: Record<IndexBand, string> = {
  low: 'podprůměr',
  mid: 'průměr',
  high: 'nadprůměr',
};

const fmtIndexValue = (v: number | null): string =>
  v == null
    ? '—'
    : v.toLocaleString('cs-CZ', {
        minimumFractionDigits: 1,
        maximumFractionDigits: 1,
      });

export interface CityIndexStripProps {
  quality: CityQuality | null | undefined;
  /** Recede with a delisted card, matching the surrounding treatment. */
  muted?: boolean;
  className?: string;
}

export default function CityIndexStrip({
  quality,
  muted = false,
  className,
}: CityIndexStripProps) {
  // Not in a curated city (about half of all properties). Render nothing —
  // no placeholder row, so the card keeps its height and the absence never
  // reads as "this city scores badly".
  if (!quality || quality.readings.length === 0) return null;

  return (
    <div
      className={[
        'mt-2 grid gap-px border-t border-[var(--color-rule-soft)] pt-1.5',
        className ?? '',
      ].join(' ')}
      style={{
        gridTemplateColumns: `repeat(${quality.readings.length}, minmax(0, 1fr))`,
        opacity: muted ? 0.55 : undefined,
      }}
      role="group"
      aria-label={`Kvalita obce ${quality.city_name}`}
    >
      {quality.readings.map((r) => {
        const band = bandForValue(r.value, r.def);
        const label = indexLabel(r.def);
        const title =
          r.value == null
            ? `${label}: bez údaje (${quality.city_name})`
            : `${label}: ${fmtIndexValue(r.value)} / ${r.def.scale_max} — ${
                band ? BAND_WORD[band] : ''
              } (${quality.city_name})`;
        return (
          <span
            key={r.index_name}
            title={title}
            aria-label={title}
            className="flex min-w-0 items-baseline gap-1 border-b-2 pb-0.5"
            style={{
              // A cell with no reading gets the softest rule available rather
              // than a band colour — absent must not look like mid-scale.
              borderColor: band ? INDEX_BAND_VAR[band] : 'var(--color-rule)',
            }}
          >
            <span className="text-[0.55rem] tracking-[0.1em] uppercase text-[var(--color-ink-4)]">
              {CARD_INDEX_ABBR[r.index_name] ?? r.index_name.slice(0, 2).toUpperCase()}
            </span>
            <span className="truncate font-mono tabular-nums text-[0.66rem] text-[var(--color-ink-2)]">
              {fmtIndexValue(r.value)}
            </span>
          </span>
        );
      })}
    </div>
  );
}
