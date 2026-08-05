/* One vocabulary for every chart axis in the app: calendar-aligned time ticks
 * whose labels cannot repeat, and magnitude-aware value labels.
 *
 * Why not lean on recharts' own tick generation: for a numeric axis it spreads
 * ticks evenly over the raw millisecond domain (recharts-scale
 * `getTickValues`), so a 37-day span puts two ticks inside the same month and a
 * month-grained formatter prints "červenec 26" twice — the same class of bug as
 * a value axis printing "3,8 M" for both 3,75 M and 3,80 M. Calendar units are
 * irregular in milliseconds (28–31 day months, 23/25 hour DST days), so the
 * step has to be chosen from a ladder and walked in civil time. That is what
 * d3-time does; this is the same algorithm kept in-repo (no new dependency) and
 * pinned to one timezone so a chart reads identically for every viewer and the
 * tests are timezone-independent.
 *
 * Invariant the tests enforce: within one axis, two ticks never render the same
 * label — the label granularity is derived from the chosen step, never fixed. */

/* Czech market data, Czech operator: civil time is Prague everywhere. */
export const CHART_TZ = 'Europe/Prague';

export type TimeUnit = 'minute' | 'hour' | 'day' | 'month' | 'year';

export interface TimeAxisSpec {
  unit: TimeUnit;
  /** Step size in `unit`s between ticks (e.g. unit 'day', step 7 = weekly). */
  step: number;
  ticks: number[];
  /** Axis label — the coarsest form that is still unambiguous in this domain. */
  formatTick: (t: number) => string;
  /** Tooltip label — always fully qualified (day, month, year [, time]). */
  formatFull: (t: number) => string;
}

export interface TimeAxisOptions {
  /** Preferred tick count; the ladder picks the finest step that fits. */
  targetTicks?: number;
  /* Granularity of `formatFull`, defaulting to the tick unit. Set 'minute' for
   * a series of instants (scrape runs, events) whose exact time matters even
   * when the axis is ticked in days. */
  fullUnit?: TimeUnit;
  tz?: string;
}

/* -------------------------------------------------------------------------- */
/* Civil time in a fixed zone                                                 */
/* -------------------------------------------------------------------------- */

interface Civil {
  year: number;
  month: number; // 1-12
  day: number;
  hour: number;
  minute: number;
}

const MINUTE_MS = 60_000;

const partsFormatters = new Map<string, Intl.DateTimeFormat>();

function partsFormatter(tz: string): Intl.DateTimeFormat {
  let f = partsFormatters.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat('en-US', {
      timeZone: tz,
      hourCycle: 'h23',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
    partsFormatters.set(tz, f);
  }
  return f;
}

function toCivil(t: number, tz: string): Civil {
  const parts = partsFormatter(tz).formatToParts(new Date(t));
  const get = (type: Intl.DateTimeFormatPartTypes): number => {
    const p = parts.find((x) => x.type === type);
    return p ? Number(p.value) : 0;
  };
  return {
    year: get('year'),
    month: get('month'),
    day: get('day'),
    hour: get('hour'),
    minute: get('minute'),
  };
}

/** Offset of `tz` at instant `t`, in ms (zone clock − UTC clock). */
function zoneOffset(t: number, tz: string): number {
  const c = toCivil(t, tz);
  return (
    Date.UTC(c.year, c.month - 1, c.day, c.hour, c.minute) -
    Math.floor(t / MINUTE_MS) * MINUTE_MS
  );
}

/* Civil wall-clock -> epoch ms. Two passes so a boundary that sits on the far
 * side of a DST shift resolves to the offset actually in force there. */
function fromCivil(c: Civil, tz: string): number {
  const asUtc = Date.UTC(c.year, c.month - 1, c.day, c.hour, c.minute);
  const first = zoneOffset(asUtc, tz);
  const t = asUtc - first;
  const second = zoneOffset(t, tz);
  return second === first ? t : asUtc - second;
}

/* Field arithmetic with carry, done on a UTC carrier date so month lengths and
 * leap years normalise for free. Ticks are always re-resolved through
 * fromCivil(), so DST never accumulates drift the way ms addition would. */
function addCivil(c: Civil, unit: TimeUnit, step: number): Civil {
  const d = new Date(Date.UTC(c.year, c.month - 1, c.day, c.hour, c.minute));
  if (unit === 'minute') d.setUTCMinutes(d.getUTCMinutes() + step);
  else if (unit === 'hour') d.setUTCHours(d.getUTCHours() + step);
  else if (unit === 'day') d.setUTCDate(d.getUTCDate() + step);
  else if (unit === 'month') d.setUTCMonth(d.getUTCMonth() + step);
  else d.setUTCFullYear(d.getUTCFullYear() + step);
  return {
    year: d.getUTCFullYear(),
    month: d.getUTCMonth() + 1,
    day: d.getUTCDate(),
    hour: d.getUTCHours(),
    minute: d.getUTCMinutes(),
  };
}

function weekdayMon0(c: Civil): number {
  const dow = new Date(Date.UTC(c.year, c.month - 1, c.day)).getUTCDay();
  return (dow + 6) % 7; // Monday = 0, Czech week start
}

/** Largest boundary of (unit, step) at or before `c`. */
function floorCivil(c: Civil, unit: TimeUnit, step: number): Civil {
  if (unit === 'minute') return { ...c, minute: c.minute - (c.minute % step) };
  if (unit === 'hour') return { ...c, minute: 0, hour: c.hour - (c.hour % step) };
  if (unit === 'day') {
    const midnight = { ...c, hour: 0, minute: 0 };
    // Multi-week steps anchor on Monday; 1- and 2-day steps take the phase of
    // the domain start (anchoring those on the calendar would jump at month
    // ends, which reads as a broken axis).
    return step % 7 === 0 ? addCivil(midnight, 'day', -weekdayMon0(midnight)) : midnight;
  }
  if (unit === 'month') {
    return { ...c, day: 1, hour: 0, minute: 0, month: c.month - ((c.month - 1) % step) };
  }
  return { year: c.year - (((c.year % step) + step) % step), month: 1, day: 1, hour: 0, minute: 0 };
}

/* -------------------------------------------------------------------------- */
/* Step ladder                                                                */
/* -------------------------------------------------------------------------- */

const APPROX_MS: Record<TimeUnit, number> = {
  minute: MINUTE_MS,
  hour: 3_600_000,
  day: 86_400_000,
  month: 2_629_746_000,
  year: 31_556_952_000,
};

/* Human-readable calendar steps, no gap wider than ~2.5x — with the
 * round-up rule below, a wide gap would leave a sparse axis (a 14-day window
 * jumping straight from 2-day to weekly ticks yields two labels). Months stay
 * on 1/3/6 so month ticks read as months, quarters, and halves. */
const LADDER: readonly (readonly [TimeUnit, number])[] = [
  ['minute', 1], ['minute', 2], ['minute', 5], ['minute', 10], ['minute', 15], ['minute', 30],
  ['hour', 1], ['hour', 2], ['hour', 3], ['hour', 6], ['hour', 12],
  ['day', 1], ['day', 2], ['day', 3], ['day', 7], ['day', 14],
  ['month', 1], ['month', 3], ['month', 6],
  ['year', 1], ['year', 2], ['year', 5], ['year', 10], ['year', 25], ['year', 50], ['year', 100],
];

const MAX_TICKS = 400;

/* Finest ladder entry that still fits within `targetTicks`. Rounding up rather
 * than to the nearest entry keeps the tick count at or below the target: Czech
 * date labels are wide ("červenec 26"), so an axis is far more readable a
 * little sparse than a little crowded. */
function pickStep(spanMs: number, targetTicks: number): readonly [TimeUnit, number] {
  const wanted = spanMs / Math.max(1, targetTicks);
  return LADDER.find(([unit, step]) => APPROX_MS[unit] * step >= wanted) ?? LADDER[LADDER.length - 1];
}

/* -------------------------------------------------------------------------- */
/* Labels                                                                     */
/* -------------------------------------------------------------------------- */

const dtfCache = new Map<string, Intl.DateTimeFormat>();

function dtf(tz: string, opts: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `${tz}|${JSON.stringify(opts)}`;
  let f = dtfCache.get(key);
  if (!f) {
    f = new Intl.DateTimeFormat('cs-CZ', { timeZone: tz, ...opts });
    dtfCache.set(key, f);
  }
  return f;
}

/* The label carries exactly the fields needed to stay unique at this step:
 * finer than a day -> clock time (plus the date once the domain crosses a day),
 * day/week -> "27. 6." (plus the year once it crosses a year), and so on. */
function tickOptions(unit: TimeUnit, multiDay: boolean, multiYear: boolean): Intl.DateTimeFormatOptions {
  if (unit === 'year') return { year: 'numeric' };
  if (unit === 'month') return { month: 'short', year: '2-digit' };
  if (unit === 'day') {
    return multiYear
      ? { day: 'numeric', month: 'numeric', year: '2-digit' }
      : { day: 'numeric', month: 'numeric' };
  }
  return multiDay
    ? { day: 'numeric', month: 'numeric', hour: '2-digit', minute: '2-digit' }
    : { hour: '2-digit', minute: '2-digit' };
}

function fullOptions(unit: TimeUnit): Intl.DateTimeFormatOptions {
  const date: Intl.DateTimeFormatOptions = { day: 'numeric', month: 'numeric', year: 'numeric' };
  return unit === 'minute' || unit === 'hour'
    ? { ...date, hour: '2-digit', minute: '2-digit' }
    : date;
}

/** Terse label for an already-bucketed series (Costs, dedup timeline). */
export function timeLabel(t: number | string, unit: TimeUnit, tz: string = CHART_TZ): string {
  const ms = toMs(t);
  if (ms == null) return '—';
  return dtf(tz, tickOptions(unit, true, false)).format(ms);
}

/** Fully-qualified label for tooltips over an already-bucketed series. */
export function timeLabelFull(t: number | string, unit: TimeUnit, tz: string = CHART_TZ): string {
  const ms = toMs(t);
  if (ms == null) return '—';
  return dtf(tz, fullOptions(unit)).format(ms);
}

/* Bucket keys arrive as epoch ms, full ISO timestamps, or bare 'YYYY-MM-DD'
 * days (which JS would otherwise read as UTC midnight and, west of Greenwich,
 * render as the previous day). */
function toMs(t: number | string): number | null {
  if (typeof t === 'number') return Number.isFinite(t) ? t : null;
  const iso = /^\d{4}-\d{2}-\d{2}$/.test(t) ? `${t}T00:00:00` : t;
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? null : ms;
}

/* -------------------------------------------------------------------------- */
/* Public: time axis                                                          */
/* -------------------------------------------------------------------------- */

export function timeAxisSpec(
  domain: readonly [number, number],
  { targetTicks = 6, fullUnit, tz = CHART_TZ }: TimeAxisOptions = {},
): TimeAxisSpec {
  const [min, max] = domain;
  const valid = Number.isFinite(min) && Number.isFinite(max) && max >= min;
  const span = valid ? max - min : 0;
  const [unit, step] = pickStep(Math.max(span, 1), targetTicks);

  const ticks: number[] = [];
  if (valid) {
    let civil = floorCivil(toCivil(min, tz), unit, step);
    let t = fromCivil(civil, tz);
    for (let guard = 0; t < min && guard < MAX_TICKS; guard++) {
      civil = addCivil(civil, unit, step);
      t = fromCivil(civil, tz);
    }
    while (t <= max && ticks.length < MAX_TICKS) {
      ticks.push(t);
      civil = addCivil(civil, unit, step);
      t = fromCivil(civil, tz);
    }
  }
  // Degenerate domains (a single observation, or a span shorter than the
  // finest step) still deserve an axis: fall back to the endpoints.
  if (ticks.length === 0) ticks.push(...(valid ? (span === 0 ? [min] : [min, max]) : []));

  const first = ticks.length ? toCivil(ticks[0], tz) : null;
  const last = ticks.length ? toCivil(ticks[ticks.length - 1], tz) : null;
  const multiYear = !!first && !!last && first.year !== last.year;
  const multiDay =
    !!first && !!last && (multiYear || first.month !== last.month || first.day !== last.day);

  const tickFmt = dtf(tz, tickOptions(unit, multiDay, multiYear));
  const fullFmt = dtf(tz, fullOptions(fullUnit ?? unit));
  return {
    unit,
    step,
    ticks,
    formatTick: (t: number) => (Number.isFinite(t) ? tickFmt.format(t) : '—'),
    formatFull: (t: number) => (Number.isFinite(t) ? fullFmt.format(t) : '—'),
  };
}

/* -------------------------------------------------------------------------- */
/* Public: value axis                                                         */
/* -------------------------------------------------------------------------- */

export interface ValueAxisOptions {
  /** Preferred tick count; the nice-number step lands at or below it. */
  targetTicks?: number;
  /** Anchor the axis at 0 — right for bars, misleading for a narrow price band. */
  zeroBased?: boolean;
  /** Counts, not quantities: never step by a fraction. */
  integer?: boolean;
  /** Currency mark glued to the number, e.g. '$'. */
  prefix?: string;
  locale?: string;
}

export interface ValueAxisSpec {
  /** Hand this to the axis together with `ticks` — the two belong together. */
  domain: [number, number];
  ticks: number[];
  format: (v: number) => string;
}

const NBSP = ' ';

const SCALES: readonly (readonly [number, string])[] = [
  [1_000_000_000, 'mld.'],
  [1_000_000, 'M'],
  [1_000, 'k'],
  [1, ''],
];

const numberFormatters = new Map<string, Intl.NumberFormat>();

function nf(locale: string, decimals: number): Intl.NumberFormat {
  const key = `${locale}|${decimals}`;
  let f = numberFormatters.get(key);
  if (!f) {
    f = new Intl.NumberFormat(locale, {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    });
    numberFormatters.set(key, f);
  }
  return f;
}

/* Nice-number step (1/2/5 x 10^n), the standard axis rounding. */
function niceStep(rawStep: number, integer: boolean): number {
  if (!(rawStep > 0)) return 1;
  const magnitude = Math.pow(10, Math.floor(Math.log10(rawStep)));
  const normalised = rawStep / magnitude;
  const multiple = normalised <= 1 ? 1 : normalised <= 2 ? 2 : normalised <= 5 ? 5 : 10;
  const step = multiple * magnitude;
  return integer ? Math.max(1, Math.round(step)) : step;
}

/* One scale unit for the whole axis (mixing "950 k" with "1 M" on one axis
 * misreads badly) and exactly enough decimals to resolve one step, so two
 * ticks can never collapse onto the same label. */
function compactFormatter(
  hi: number,
  step: number,
  prefix: string,
  locale: string,
): (v: number) => string {
  const [divisor, suffix] = SCALES.find(([d]) => hi >= d) ?? SCALES[SCALES.length - 1];
  const decimals = Math.min(3, Math.max(0, Math.ceil(Math.log10(divisor / step) - 1e-9)));
  const fmt = nf(locale, decimals);
  return (v: number) =>
    Number.isFinite(v) ? `${prefix}${fmt.format(v / divisor)}${suffix ? NBSP + suffix : ''}` : '—';
}

/* Domain, ticks and labels decided together. Letting the chart library round
 * the domain on its own and formatting against the raw data range is what
 * printed "3,8 M" for both 3,75 M and 3,80 M: the label precision has to come
 * from the step actually rendered. */
export function valueAxisSpec(
  dataDomain: readonly [number, number],
  {
    targetTicks = 5,
    zeroBased = false,
    integer = false,
    prefix = '',
    locale = 'cs-CZ',
  }: ValueAxisOptions = {},
): ValueAxisSpec {
  const finite = Number.isFinite(dataDomain[0]) && Number.isFinite(dataDomain[1]);
  let min = finite ? Math.min(dataDomain[0], dataDomain[1]) : 0;
  let max = finite ? Math.max(dataDomain[0], dataDomain[1]) : 0;
  if (zeroBased) min = Math.min(0, min);
  if (max === min) {
    // A flat series still needs a band to sit in.
    const pad = integer ? 1 : Math.abs(max) * 0.1 || 1;
    max += pad;
    if (!zeroBased) min -= pad;
  }

  const step = niceStep((max - min) / Math.max(1, targetTicks), integer);
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const count = Math.round((hi - lo) / step);
  const ticks: number[] = [];
  for (let i = 0; i <= count; i++) {
    // Rebuild from the index rather than accumulating, or 0.1-sized steps drift.
    ticks.push(Number((lo + i * step).toPrecision(12)));
  }

  return {
    domain: [lo, hi],
    ticks,
    format: compactFormatter(Math.max(Math.abs(lo), Math.abs(hi)), step, prefix, locale),
  };
}

/** Min/max across the given numeric keys of a row set — the input to valueAxisSpec. */
export function numericDomain<T extends Record<string, unknown>>(
  rows: readonly T[],
  keys: readonly string[],
): [number, number] {
  let min = Infinity;
  let max = -Infinity;
  for (const row of rows) {
    for (const key of keys) {
      const v = row[key];
      if (typeof v === 'number' && Number.isFinite(v)) {
        if (v < min) min = v;
        if (v > max) max = v;
      }
    }
  }
  return min === Infinity ? [0, 0] : [min, max];
}
