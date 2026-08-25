import type { DerivedArtifactRow } from './types';
import { fmtCount, fmtDurationSecs } from './format';

/* Presentation + freshness rollup for the derived-artifact registry
 * (migration 437, `derived_artifacts_public`). One row per matview / rollup
 * table: who produces it, on what cadence, how stale it may get, when it last
 * succeeded.
 *
 * Unlike pipeline_checks — where the DB stamps ok/warn/fail and these helpers
 * only humanize it — the registry publishes FACTS and the status is derived
 * here, from `last_succeeded_at` against `staleness_budget`. That is deliberate:
 * the view exposes no error column at all (see types.ts), so "it stopped
 * succeeding" is the whole signal. */

export type DerivedArtifactStatus = 'ok' | 'stale' | 'never' | 'paused' | 'unknown';

const SECOND = 1_000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/* Postgres' own approximations, which is what the budgets are written in.
 * A staleness budget is never fine-grained enough for the difference to
 * matter. */
const UNIT_MS: Record<string, number> = {
  us: SECOND / 1_000_000, usec: SECOND / 1_000_000, usecs: SECOND / 1_000_000,
  microsecond: SECOND / 1_000_000, microseconds: SECOND / 1_000_000,
  ms: 1, msec: 1, msecs: 1, millisecond: 1, milliseconds: 1,
  s: SECOND, sec: SECOND, secs: SECOND, second: SECOND, seconds: SECOND,
  m: MINUTE, min: MINUTE, mins: MINUTE, minute: MINUTE, minutes: MINUTE,
  h: HOUR, hr: HOUR, hrs: HOUR, hour: HOUR, hours: HOUR,
  d: DAY, day: DAY, days: DAY,
  w: 7 * DAY, week: 7 * DAY, weeks: 7 * DAY,
  mon: 30 * DAY, mons: 30 * DAY, month: 30 * DAY, months: 30 * DAY,
  y: 365 * DAY, yr: 365 * DAY, yrs: 365 * DAY, year: 365 * DAY, years: 365 * DAY,
};

const ISO_RE =
  /^([+-])?P(?:([\d.]+)Y)?(?:([\d.]+)M)?(?:([\d.]+)W)?(?:([\d.]+)D)?(?:T(?:([\d.]+)H)?(?:([\d.]+)M)?(?:([\d.]+)S)?)?$/i;
const VERBOSE_RE = /(-?[\d.]+)\s*([a-z]+)/g;
const CLOCK_RE = /(^|\s)(-)?(\d+):(\d{2})(?::(\d{2}(?:\.\d+)?))?(?=\s|$)/;

/* An interval crosses the wire as a STRING whose shape depends on the server's
 * IntervalStyle. Production runs `postgres` (verified live), so every budget
 * under a day arrives as 'HH:MM:SS' — but the same setting switches to a
 * '1 day 00:00:00' / '1 mon 5 days' prefix past 24 h, so a registry row with a
 * longer budget looks nothing like the three that exist today. 'PT1H'
 * (iso_8601) and '45 mins' (verbose) are handled too, in case the setting
 * moves.
 *
 * Anything else returns NULL rather than a guess. Defaulting an unparsed
 * budget to 0 would mark every artifact permanently stale and defaulting it to
 * Infinity would mark a dead one healthy — both silently. Null surfaces as
 * 'unknown'. */
export function parseIntervalMs(raw: unknown): number | null {
  if (typeof raw !== 'string') return null;
  const s = raw.trim().toLowerCase();
  if (!s) return null;

  const iso = ISO_RE.exec(s);
  if (iso && iso.slice(2).some((g) => g !== undefined)) {
    const [, sign, y, mo, w, d, h, mi, sec] = iso;
    const num = (v: string | undefined) => (v ? Number(v) : 0);
    const ms =
      num(y) * UNIT_MS.year +
      num(mo) * UNIT_MS.month +
      num(w) * UNIT_MS.week +
      num(d) * UNIT_MS.day +
      num(h) * UNIT_MS.hour +
      num(mi) * UNIT_MS.minute +
      num(sec) * UNIT_MS.second;
    return sign === '-' ? -ms : ms;
  }

  let ms = 0;
  let matched = false;

  const clock = CLOCK_RE.exec(s);
  if (clock) {
    const [, , neg, hh, mm, ss] = clock;
    const abs =
      Number(hh) * UNIT_MS.hour + Number(mm) * UNIT_MS.minute + Number(ss ?? 0) * UNIT_MS.second;
    ms += neg ? -abs : abs;
    matched = true;
  }

  VERBOSE_RE.lastIndex = 0;
  for (let m = VERBOSE_RE.exec(s); m; m = VERBOSE_RE.exec(s)) {
    const unit = UNIT_MS[m[2]];
    if (unit === undefined) continue;
    const n = Number(m[1]);
    if (!Number.isFinite(n)) continue;
    ms += n * unit;
    matched = true;
  }

  return matched ? ms : null;
}

/* Object names read better verbatim than title-cased, so the fallback only
 * softens the underscores; the raw name stays available as the row's title. */
const ARTIFACT_LABELS: Record<string, string> = {
  llm_cost_hour_rollup: 'LLM cost rollup',
  browse_list: 'Browse list',
  properties_map_mv: 'Browse map',
};

export function derivedArtifactLabel(name: string): string {
  const known = ARTIFACT_LABELS[name];
  if (known) return known;
  const spaced = name.replace(/_/g, ' ');
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

export interface DerivedArtifactFreshness {
  status: DerivedArtifactStatus;
  /* Parsed `staleness_budget`, ms. Null when the interval didn't parse. */
  budgetMs: number | null;
  /* now − last_succeeded_at, ms. Null when it has never succeeded. */
  ageMs: number | null;
}

/* Precedence, worst-signal-last-known-first:
 *   paused  — is_serving = false. Nothing is producing it ON PURPOSE, so an
 *             overdue timestamp is expected and must not read as an alarm.
 *   never   — no last_succeeded_at at all: registered but never produced.
 *   unknown — the budget didn't parse, so staleness is genuinely unknowable
 *             here; better shown than silently called ok.
 *   stale   — older than its budget. STRICTLY older: a budget is the largest
 *             age still considered fresh, so age === budget is ok.
 *   ok      — within budget. */
export function derivedArtifactFreshness(
  row: DerivedArtifactRow,
  now: Date,
): DerivedArtifactFreshness {
  const budgetMs = parseIntervalMs(row.staleness_budget);
  const succeeded = row.last_succeeded_at ? new Date(row.last_succeeded_at).getTime() : NaN;
  const ageMs = Number.isNaN(succeeded) ? null : now.getTime() - succeeded;

  if (!row.is_serving) return { status: 'paused', budgetMs, ageMs };
  if (ageMs === null) return { status: 'never', budgetMs, ageMs };
  if (budgetMs === null) return { status: 'unknown', budgetMs, ageMs };
  return { status: ageMs > budgetMs ? 'stale' : 'ok', budgetMs, ageMs };
}

export const derivedArtifactStatus = (row: DerivedArtifactRow, now: Date): DerivedArtifactStatus =>
  derivedArtifactFreshness(row, now).status;

/* The right-hand value column: how big the artifact was on its last run. */
export const derivedArtifactValueLabel = (row: DerivedArtifactRow): string =>
  row.last_rows == null ? '—' : fmtCount(row.last_rows);

/* The hover explanation — producer, host, cadence, budget, last duration.
 * Everything the registry knows that the row itself has no room for. */
export function derivedArtifactTitle(row: DerivedArtifactRow, now: Date): string {
  const { budgetMs } = derivedArtifactFreshness(row, now);
  const parts = [
    row.name,
    `${row.producer} · ${row.host}`,
    `cadence ${row.cadence}`,
    `budget ${budgetMs == null ? (row.staleness_budget ?? '—') : fmtDurationSecs(budgetMs / 1000)}`,
  ];
  if (row.last_duration_ms != null) {
    parts.push(`last run ${fmtDurationSecs(row.last_duration_ms / 1000)}`);
  }
  if (!row.is_serving) parts.push('not serving');
  return parts.join('\n');
}

export interface DerivedArtifactsSummary {
  ok: number;
  stale: number;
  never: number;
  paused: number;
  unknown: number;
  worst: DerivedArtifactStatus;
}

/* A stale artifact is serving wrong data right now, so it outranks one that has
 * never run. `worst` is a pure ranking — the panel's badge reads the counts it
 * cares about, so a paused-only registry ranks 'paused' without alarming. */
const WORST_ORDER: DerivedArtifactStatus[] = ['stale', 'never', 'unknown', 'paused', 'ok'];

export function summarizeDerivedArtifacts(
  rows: DerivedArtifactRow[],
  now: Date,
): DerivedArtifactsSummary {
  const acc: Record<DerivedArtifactStatus, number> = {
    ok: 0, stale: 0, never: 0, paused: 0, unknown: 0,
  };
  for (const r of rows) acc[derivedArtifactStatus(r, now)] += 1;
  const worst = WORST_ORDER.find((s) => acc[s] > 0) ?? 'ok';
  return { ...acc, worst };
}

// stale first, then never, unknown, paused, ok; stable by name inside a band.
const STATUS_ORDER: Record<DerivedArtifactStatus, number> = {
  stale: 0, never: 1, unknown: 2, paused: 3, ok: 4,
};

export function sortDerivedArtifacts(
  rows: DerivedArtifactRow[],
  now: Date,
): DerivedArtifactRow[] {
  return [...rows].sort(
    (a, b) =>
      STATUS_ORDER[derivedArtifactStatus(a, now)] - STATUS_ORDER[derivedArtifactStatus(b, now)] ||
      a.name.localeCompare(b.name),
  );
}
