/* Czech-locale formatters. Centralised so a price, area, count, or
 * timestamp looks the same wherever it appears. */

const NBSP = ' ';
const THIN_SPACE = ' ';

const czNumber = new Intl.NumberFormat('cs-CZ');
const czNumberCompact = new Intl.NumberFormat('cs-CZ', {
  notation: 'compact',
  maximumFractionDigits: 1,
});

const czShortDate = new Intl.DateTimeFormat('cs-CZ', {
  day: 'numeric',
  month: 'numeric',
});

/* Czech short date — "5. 5." (day. month.). Used on listing-card
 * badges where the year is implicit and screen real estate is tiny. */
export const fmtShortDate = (iso: string | null | undefined): string => {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '—';
  return czShortDate.format(d);
};

/* Czech-plural day count: 1 den, 2-4 dny, 5+ dní. Negative or null -> "—". */
export const fmtTomDays = (n: number | null | undefined): string => {
  if (n == null || n < 0) return '—';
  const noun =
    n === 1 ? 'den' :
    n >= 2 && n <= 4 ? 'dny' :
    'dní';
  return `${czNumber.format(n)}${NBSP}${noun}`;
};

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

/* Human-readable elapsed duration from a raw seconds count — "45 s", "12 min",
 * "3 h 20 min", "2 d 4 h". For queue-age / latency gauges (not a wall-clock).
 * Null / negative / non-finite -> "—". */
export const fmtDurationSecs = (secs: number | null | undefined): string => {
  if (secs == null || !Number.isFinite(secs) || secs < 0) return '—';
  const s = Math.round(secs);
  if (s < MIN) return `${s}${THIN_SPACE}s`;
  if (s < HOUR) return `${Math.round(s / MIN)}${THIN_SPACE}min`;
  if (s < DAY) {
    const h = Math.floor(s / HOUR);
    const m = Math.round((s % HOUR) / MIN);
    return m > 0 ? `${h}${THIN_SPACE}h ${m}${THIN_SPACE}min` : `${h}${THIN_SPACE}h`;
  }
  const d = Math.floor(s / DAY);
  const h = Math.round((s % DAY) / HOUR);
  return h > 0 ? `${d}${THIN_SPACE}d ${h}${THIN_SPACE}h` : `${d}${THIN_SPACE}d`;
};

/* Migration 022 fields. The slug→Czech-label mapping lives in
 * lib/enums.ts; these formatters are the friendly wrappers that fall
 * back to '—' for nulls. */

import {
  CATEGORY_SUB_LABELS,
  FURNISHED_LABELS,
  OWNERSHIP_LABELS,
  categorySubLabel,
} from './enums';
import type { Furnished, Ownership } from './types';

export const fmtFurnished = (f: Furnished | null | undefined): string =>
  f == null ? '—' : FURNISHED_LABELS[f];

export const fmtOwnership = (o: Ownership | null | undefined): string =>
  o == null ? '—' : OWNERSHIP_LABELS[o];

export const fmtParkingLots = (n: number | null | undefined): string =>
  n == null ? '—' : `${czNumber.format(n)}${NBSP}${n === 1 ? 'místo' : 'místa'}`;

export const fmtCategorySub = (cb: number | null | undefined): string =>
  cb == null ? '—' : (categorySubLabel(cb) ?? '—');

/* Re-export the label dict so dropdowns can iterate values without
 * a second import. */
export { CATEGORY_SUB_LABELS, FURNISHED_LABELS, OWNERSHIP_LABELS };

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

const pad2 = (n: number) => String(n).padStart(2, '0');

export const fmtDateSlash = (iso: string | null | undefined): string => {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '—';
  return `${pad2(d.getDate())}/${pad2(d.getMonth() + 1)}/${d.getFullYear()}`;
};

export const fmtTime24 = (iso: string | null | undefined): string => {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '—';
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
};
