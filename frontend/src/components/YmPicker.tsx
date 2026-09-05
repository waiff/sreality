/* Year/month (YYYY-MM) picker — two civic-archive selects behind one named
 * group. THE year/month picker: three byte-similar copies existed (this one,
 * ListingMap's PsgYmPicker, pages/Datasets' page-local), and all three shipped
 * two selects with an accessible name of "". A fourth must not appear —
 * eslint.config.js bans a local `function YmPicker` the way it bans `Field`. */

const FIRST_YEAR = 2015;
const NOW = new Date();
const YEARS = Array.from(
  { length: NOW.getFullYear() - FIRST_YEAR + 1 },
  (_, i) => String(FIRST_YEAR + i),
);
const MONTHS = Array.from({ length: 12 }, (_, i) => String(i + 1).padStart(2, '0'));

/* The two scales the three former copies shipped. 'sm' was YM_SELECT_CLS and
 * ListingMap's byte-identical PSG_SELECT_CLS; 'md' was pages/Datasets'
 * SELECT_CLS. A closed union, not a className passthrough, so the widget can
 * never be restyled into a fourth look. YM_SELECT_CLS stays exported: the
 * growth-dataset select in ListingMap wears it too. */
export const YM_SELECT_CLS =
  'text-[0.7rem] bg-[var(--color-paper-2)] border border-[var(--color-rule)] rounded px-1 py-0.5';
const YM_SELECT_CLS_MD =
  'text-sm border border-[var(--color-rule)] rounded-[var(--radius-sm)] ' +
  'bg-[var(--color-paper-3)] px-2.5 py-1.5 text-[var(--color-ink)]';

/* The part words. Two selects need two distinct names under one caption, and
 * the mount surfaces are not in one language: the map overlay is Czech, the
 * Browse sidebar and the Datasets page are English. Two frozen pairs rather
 * than free-text props, so there are exactly two vocabularies. */
export const YM_PARTS = {
  cs: { year: 'rok', month: 'měsíc' },
  en: { year: 'year', month: 'month' },
} as const;

/* Current month as 'YYYY-MM' — the natural open end of a scrape window. */
export const YM_CUR = `${NOW.getFullYear()}-${String(NOW.getMonth() + 1).padStart(2, '0')}`;

// eslint-disable-next-line no-restricted-syntax -- THE YmPicker; the ban exists so a fourth local copy cannot appear
export function YmPicker({
  label,
  value,
  onChange,
  size = 'sm',
  parts = YM_PARTS.cs,
}: {
  /* REQUIRED, and that is the point. Two selects cannot share one <label>
   * wrap, and a role="group" name is ancestor context that never becomes a
   * descendant select's accessible name — so the caption has to arrive as
   * WORDS and be spent on each select's aria-label. Required makes `tsc`
   * enumerate every mount site, now and forever. */
  label: string;
  /** 'YYYY-MM'. Empty/malformed falls back to `${FIRST_YEAR}-01`. */
  value: string;
  onChange?: (v: string) => void;
  size?: 'sm' | 'md';
  parts?: { year: string; month: string };
}) {
  const cls = size === 'md' ? YM_SELECT_CLS_MD : YM_SELECT_CLS;
  const [y, m] = (value || `${FIRST_YEAR}-01`).split('-');
  return (
    <span
      role="group"
      aria-label={label}
      className={`inline-flex items-center ${size === 'md' ? 'gap-1' : 'gap-0.5'}`}
    >
      <select
        aria-label={`${label} – ${parts.year}`}
        value={y}
        onChange={(e) => onChange?.(`${e.target.value}-${m}`)}
        className={cls}
      >
        {YEARS.map((yr) => (
          <option key={yr} value={yr}>
            {yr}
          </option>
        ))}
      </select>
      <select
        aria-label={`${label} – ${parts.month}`}
        value={m}
        onChange={(e) => onChange?.(`${y}-${e.target.value}`)}
        className={cls}
      >
        {MONTHS.map((mo) => (
          <option key={mo} value={mo}>
            {mo}
          </option>
        ))}
      </select>
    </span>
  );
}
