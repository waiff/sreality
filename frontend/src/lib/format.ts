/* Czech-locale formatters. Centralised so a price, area, count, or
 * timestamp looks the same wherever it appears. */

const NBSP = ' ';
const THIN_SPACE = ' ';

const czNumber = new Intl.NumberFormat('cs-CZ');
const czNumberCompact = new Intl.NumberFormat('cs-CZ', {
  notation: 'compact',
  maximumFractionDigits: 1,
});

export const fmtCount = (n: number | null | undefined): string =>
  n == null ? '—' : czNumber.format(n);

export const fmtCountCompact = (n: number | null | undefined): string =>
  n == null ? '—' : czNumberCompact.format(n);

export const fmtCzk = (n: number | null | undefined): string =>
  n == null ? '—' : `${czNumber.format(n)}${NBSP}Kč`;

export const fmtArea = (n: number | null | undefined): string =>
  n == null ? '—' : `${czNumber.format(Math.round(n))}${NBSP}m²`;

export const fmtPricePerM2 = (
  price: number | null | undefined,
  area: number | null | undefined,
): string => {
  if (price == null || area == null || area <= 0) return '—';
  return `${czNumber.format(Math.round(price / area))}${NBSP}Kč/m²`;
};

const SEC = 1, MIN = 60, HOUR = 3600, DAY = 86400;

export const fmtRelative = (iso: string | null | undefined): string => {
  if (!iso) return '—';
  const t = new Date(iso).getTime();
  if (isNaN(t)) return '—';
  const diff = Math.max(0, (Date.now() - t) / 1000);
  if (diff < MIN)  return `${Math.round(diff / SEC)}${THIN_SPACE}s ago`;
  if (diff < HOUR) return `${Math.round(diff / MIN)}${THIN_SPACE}min ago`;
  if (diff < DAY)  return `${Math.round(diff / HOUR)}${THIN_SPACE}h ago`;
  const days = Math.round(diff / DAY);
  if (days < 14) return `${days}${THIN_SPACE}days ago`;
  if (days < 60) return `${Math.round(days / 7)}${THIN_SPACE}weeks ago`;
  if (days < 365) return `${Math.round(days / 30)}${THIN_SPACE}months ago`;
  return `${Math.round(days / 365)}${THIN_SPACE}yr ago`;
};

export const fmtAbsolute = (iso: string | null | undefined): string => {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '—';
  return d.toLocaleString('cs-CZ', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
};
