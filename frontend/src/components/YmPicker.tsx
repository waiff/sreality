/* Year/month (YYYY-MM) picker — two civic-archive selects. Shared by the
 * price-stats growth controls (Browse filter + map overlay). */
const FIRST_YEAR = 2015;
const NOW = new Date();
const YEARS = Array.from(
  { length: NOW.getFullYear() - FIRST_YEAR + 1 },
  (_, i) => String(FIRST_YEAR + i),
);
const MONTHS = Array.from({ length: 12 }, (_, i) => String(i + 1).padStart(2, '0'));

export const YM_SELECT_CLS =
  'text-[0.7rem] bg-[var(--color-paper-2)] border border-[var(--color-rule)] rounded px-1 py-0.5';

/* Current month as 'YYYY-MM' — the natural open end of a scrape window. */
export const YM_CUR = `${NOW.getFullYear()}-${String(NOW.getMonth() + 1).padStart(2, '0')}`;

export function YmPicker({
  value,
  onChange,
}: {
  value: string;
  onChange?: (v: string) => void;
}) {
  const [y, m] = (value || `${FIRST_YEAR}-01`).split('-');
  return (
    <span className="inline-flex items-center gap-0.5">
      <select value={y} onChange={(e) => onChange?.(`${e.target.value}-${m}`)} className={YM_SELECT_CLS}>
        {YEARS.map((yr) => <option key={yr} value={yr}>{yr}</option>)}
      </select>
      <select value={m} onChange={(e) => onChange?.(`${y}-${e.target.value}`)} className={YM_SELECT_CLS}>
        {MONTHS.map((mo) => <option key={mo} value={mo}>{mo}</option>)}
      </select>
    </span>
  );
}
