import { describe, expect, it } from 'vitest';
import {
  CHART_TZ,
  numericDomain,
  timeAxisSpec,
  timeLabel,
  timeLabelFull,
  valueAxisSpec,
} from './chartAxis';

const T = (iso: string) => Date.parse(iso);
const DAY = 86_400_000;
const NBSP = ' '; // the space valueAxisSpec puts before k / M

describe('timeAxisSpec — step selection', () => {
  const cases: [string, string, string, string, number][] = [
    // label,            from,                   to,                     unit,     step
    ['two hours',        '2026-06-27T08:00:00Z', '2026-06-27T10:00:00Z', 'minute', 30],
    ['two days',         '2026-06-27T08:00:00Z', '2026-06-29T08:00:00Z', 'hour',   12],
    ['ten days',         '2026-06-01T00:00:00Z', '2026-06-11T00:00:00Z', 'day',    2],
    ['the reported 37d', '2026-06-27T00:00:00Z', '2026-08-03T00:00:00Z', 'day',    7],
    ['four months',      '2026-03-01T00:00:00Z', '2026-07-01T00:00:00Z', 'month',  1],
    ['a year',           '2025-08-01T00:00:00Z', '2026-08-01T00:00:00Z', 'month',  3],
    ['eight years',      '2018-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'year',   2],
  ];
  for (const [label, from, to, unit, step] of cases) {
    it(`picks ${unit}/${step} for ${label}`, () => {
      const spec = timeAxisSpec([T(from), T(to)]);
      expect([spec.unit, spec.step]).toEqual([unit, step]);
    });
  }
});

describe('timeAxisSpec — tick placement', () => {
  it('lands ticks on calendar boundaries, never on the raw domain edges', () => {
    // The bug this module exists for: recharts spread 5 ticks evenly over the
    // millisecond domain, so two of them fell inside July.
    const spec = timeAxisSpec([T('2026-06-27T14:32:11Z'), T('2026-08-03T09:10:00Z')]);
    expect(spec.ticks.length).toBeGreaterThanOrEqual(4);
    for (const t of spec.ticks) {
      const d = new Date(t);
      // Weekly ticks -> Prague midnight (22:00Z in summer, 23:00Z in winter).
      expect(d.getUTCMinutes()).toBe(0);
      expect([21, 22, 23]).toContain(d.getUTCHours());
    }
  });

  it('anchors weekly ticks on Monday', () => {
    const spec = timeAxisSpec([T('2026-06-27T00:00:00Z'), T('2026-08-03T00:00:00Z')]);
    for (const t of spec.ticks) {
      // Read the weekday in Prague, where the tick is midnight.
      const prague = new Date(t + 2 * 3_600_000);
      expect(prague.getUTCDay()).toBe(1);
    }
  });

  it('anchors quarterly ticks on Jan/Apr/Jul/Oct', () => {
    const spec = timeAxisSpec([T('2025-08-01T00:00:00Z'), T('2026-08-01T00:00:00Z')]);
    expect(spec.unit).toBe('month');
    for (const t of spec.ticks) {
      expect([0, 3, 6, 9]).toContain(new Date(t + 2 * 3_600_000).getUTCMonth());
    }
  });

  it('keeps every tick inside the domain', () => {
    const from = T('2026-01-17T05:00:00Z');
    for (const spanDays of [0.5, 1, 3, 9, 30, 95, 400, 1500]) {
      const to = from + spanDays * DAY;
      const spec = timeAxisSpec([from, to]);
      for (const t of spec.ticks) {
        expect(t).toBeGreaterThanOrEqual(from);
        expect(t).toBeLessThanOrEqual(to);
      }
    }
  });

  it('never overshoots the target tick count, at any span', () => {
    const from = T('2026-01-17T05:00:00Z');
    for (const spanMin of [30, 120, 720, 2880, 20_000, 100_000, 500_000, 5_000_000]) {
      const spec = timeAxisSpec([from, from + spanMin * 60_000], { targetTicks: 6 });
      expect(spec.ticks.length, `span ${spanMin}m`).toBeGreaterThanOrEqual(3);
      expect(spec.ticks.length, `span ${spanMin}m`).toBeLessThanOrEqual(7);
    }
  });

  it('spaces ticks evenly in calendar terms', () => {
    const spec = timeAxisSpec([T('2026-01-01T00:00:00Z'), T('2026-12-31T00:00:00Z')]);
    const gapsDays = spec.ticks.slice(1).map((t, i) => Math.round((t - spec.ticks[i]) / DAY));
    // Quarters are 90-92 days; nothing may drift outside that.
    for (const g of gapsDays) expect(g).toBeGreaterThanOrEqual(89);
    for (const g of gapsDays) expect(g).toBeLessThanOrEqual(92);
  });
});

describe('timeAxisSpec — labels are unambiguous', () => {
  it('never repeats a label, at any span', () => {
    const from = T('2026-01-17T05:23:00Z');
    for (const spanMin of [30, 90, 240, 1440, 2880, 10_080, 53_280, 100_000, 260_000, 530_000, 2_600_000, 5_300_000]) {
      const spec = timeAxisSpec([from, from + spanMin * 60_000]);
      const labels = spec.ticks.map(spec.formatTick);
      expect(new Set(labels).size, `span ${spanMin}m -> ${labels.join(' | ')}`).toBe(labels.length);
    }
  });

  it('shows the day for a sub-month span (the reported chart)', () => {
    const spec = timeAxisSpec([T('2026-06-27T00:00:00Z'), T('2026-08-03T00:00:00Z')]);
    expect(spec.formatTick(spec.ticks[0])).toMatch(/^\d{1,2}\. \d{1,2}\.$/);
  });

  it('adds the year only once the domain crosses one', () => {
    const within = timeAxisSpec([T('2026-02-01T00:00:00Z'), T('2026-04-01T00:00:00Z')]);
    expect(within.formatTick(within.ticks[0])).toBe('9. 2.');
    const across = timeAxisSpec([T('2025-12-01T00:00:00Z'), T('2026-02-01T00:00:00Z')]);
    expect(across.formatTick(across.ticks[0])).toMatch(/^\d{1,2}\. \d{1,2}\. \d{2}$/);
  });

  it('adds the date to clock labels only once the domain crosses a day', () => {
    const oneDay = timeAxisSpec([T('2026-06-27T06:00:00Z'), T('2026-06-27T18:00:00Z')]);
    expect(oneDay.formatTick(oneDay.ticks[0])).toMatch(/^\d{2}:\d{2}$/);
    const twoDays = timeAxisSpec([T('2026-06-27T06:00:00Z'), T('2026-06-29T06:00:00Z')]);
    expect(twoDays.formatTick(twoDays.ticks[0])).toMatch(/^\d{1,2}\. \d{1,2}\. \d{2}:\d{2}$/);
  });

  it('fully qualifies tooltip labels', () => {
    const spec = timeAxisSpec([T('2026-06-27T00:00:00Z'), T('2026-08-03T00:00:00Z')]);
    expect(spec.formatFull(T('2026-07-01T10:00:00Z'))).toBe('1. 7. 2026');
    const hourly = timeAxisSpec([T('2026-06-27T06:00:00Z'), T('2026-06-27T18:00:00Z')]);
    expect(hourly.formatFull(T('2026-06-27T12:30:00Z'))).toMatch(/^27\. 6\. 2026 \d{2}:\d{2}$/);
  });
});

describe('timeAxisSpec — degenerate input', () => {
  it('returns the single instant for a zero-width domain', () => {
    const t = T('2026-06-27T00:00:00Z');
    expect(timeAxisSpec([t, t]).ticks).toEqual([t]);
  });

  it('returns no ticks for a non-finite or inverted domain', () => {
    expect(timeAxisSpec([NaN, 5]).ticks).toEqual([]);
    expect(timeAxisSpec([10, 5]).ticks).toEqual([]);
  });

  it('formats a non-finite value as an em dash rather than "Invalid Date"', () => {
    const spec = timeAxisSpec([T('2026-06-01T00:00:00Z'), T('2026-07-01T00:00:00Z')]);
    expect(spec.formatTick(NaN)).toBe('—');
    expect(spec.formatFull(NaN)).toBe('—');
  });
});

describe('timeAxisSpec — DST', () => {
  it('keeps midnight ticks at midnight across the spring shift', () => {
    // Prague springs forward on 2026-03-29.
    const spec = timeAxisSpec([T('2026-03-24T00:00:00Z'), T('2026-04-03T00:00:00Z')]);
    for (const t of spec.ticks) {
      const label = new Intl.DateTimeFormat('cs-CZ', {
        timeZone: CHART_TZ,
        hour: '2-digit',
        minute: '2-digit',
        hourCycle: 'h23',
      }).format(t);
      expect(label).toBe('00:00');
    }
  });

  it('keeps midnight ticks at midnight across the autumn shift', () => {
    const spec = timeAxisSpec([T('2026-10-20T00:00:00Z'), T('2026-11-05T00:00:00Z')]);
    for (const t of spec.ticks) {
      const label = new Intl.DateTimeFormat('cs-CZ', {
        timeZone: CHART_TZ,
        hour: '2-digit',
        minute: '2-digit',
        hourCycle: 'h23',
      }).format(t);
      expect(label).toBe('00:00');
    }
  });
});

describe('timeLabel — pre-bucketed series', () => {
  it('renders a day bucket in Prague civil time, not UTC', () => {
    expect(timeLabel('2026-06-27', 'day')).toBe('27. 6.');
    expect(timeLabel('2026-06-27T00:00:00Z', 'day')).toBe('27. 6.');
  });

  it('qualifies hour buckets with their day (48h windows repeat clock times)', () => {
    expect(timeLabel('2026-06-27T12:00:00Z', 'hour')).toMatch(/^27\. 6\. \d{2}:\d{2}$/);
    expect(timeLabelFull('2026-06-27T12:00:00Z', 'hour')).toMatch(/^27\. 6\. 2026 \d{2}:\d{2}$/);
    expect(timeLabelFull('2026-06-27', 'day')).toBe('27. 6. 2026');
  });

  it('survives an unparseable bucket', () => {
    expect(timeLabel('not-a-date', 'day')).toBe('—');
  });
});

describe('valueAxisSpec', () => {
  it('keeps neighbouring ticks distinct in a narrow price band', () => {
    // The reported Y axis: 3,70 M – 4,00 M printed "3,8 M" twice.
    const spec = valueAxisSpec([3_700_000, 4_000_000]);
    const labels = spec.ticks.map(spec.format);
    expect(new Set(labels).size).toBe(labels.length);
    expect(labels).toContain(`3,8${NBSP}M`);
    expect(labels).toContain(`4,0${NBSP}M`);
  });

  it('never repeats a label, over any band', () => {
    const bands: [number, number][] = [
      [3_700_000, 4_000_000],
      [0, 20_000_000],
      [1_190_000, 1_210_000],
      [0, 40_000],
      [0, 40],
      [0, 3],
      [12, 12],
      [990, 1_010],
      [0, 4_000_000_000],
    ];
    for (const band of bands) {
      const spec = valueAxisSpec(band);
      const labels = spec.ticks.map(spec.format);
      expect(new Set(labels).size, `${band} -> ${labels.join(' | ')}`).toBe(labels.length);
    }
  });

  it('rounds the domain out to whole ticks that cover the data', () => {
    const spec = valueAxisSpec([3_712_345, 3_998_000]);
    expect(spec.domain[0]).toBeLessThanOrEqual(3_712_345);
    expect(spec.domain[1]).toBeGreaterThanOrEqual(3_998_000);
    expect(spec.ticks[0]).toBe(spec.domain[0]);
    expect(spec.ticks[spec.ticks.length - 1]).toBe(spec.domain[1]);
    // Nice-number steps only.
    const step = spec.ticks[1] - spec.ticks[0];
    expect([1, 2, 5]).toContain(step / Math.pow(10, Math.floor(Math.log10(step))));
  });

  it('spaces every tick equally, without float drift', () => {
    const spec = valueAxisSpec([0, 1.4]);
    const gaps = spec.ticks.slice(1).map((t, i) => t - spec.ticks[i]);
    for (const g of gaps) expect(g).toBeCloseTo(gaps[0], 10);
  });

  it('anchors at zero for bars and floats for a price band', () => {
    expect(valueAxisSpec([3_700_000, 4_000_000], { zeroBased: true }).domain[0]).toBe(0);
    expect(valueAxisSpec([3_700_000, 4_000_000]).domain[0]).toBeGreaterThan(0);
  });

  it('steps by whole numbers for counts', () => {
    const spec = valueAxisSpec([0, 3], { integer: true, zeroBased: true });
    for (const t of spec.ticks) expect(Number.isInteger(t)).toBe(true);
    expect(spec.ticks[1] - spec.ticks[0]).toBeGreaterThanOrEqual(1);
  });

  it('gives a flat series a band to sit in', () => {
    const spec = valueAxisSpec([5, 5]);
    expect(spec.domain[1]).toBeGreaterThan(spec.domain[0]);
    expect(spec.ticks.length).toBeGreaterThan(1);
  });

  it('holds one scale unit for the whole axis', () => {
    const spec = valueAxisSpec([0, 1_200_000], { zeroBased: true });
    const labels = spec.ticks.map(spec.format);
    for (const label of labels) expect(label.endsWith(`${NBSP}M`)).toBe(true);
  });

  it('takes a currency prefix', () => {
    const spec = valueAxisSpec([0, 4000], { prefix: '$', zeroBased: true });
    for (const label of spec.ticks.map(spec.format)) expect(label.startsWith('$')).toBe(true);
  });

  it('formats a non-finite value as an em dash', () => {
    expect(valueAxisSpec([0, 10]).format(NaN)).toBe('—');
  });

  it('survives a non-finite domain', () => {
    const spec = valueAxisSpec([NaN, Infinity]);
    expect(spec.ticks.length).toBeGreaterThan(0);
    expect(spec.ticks.every(Number.isFinite)).toBe(true);
  });
});

describe('numericDomain', () => {
  it('spans the given keys only, ignoring nulls', () => {
    const rows = [
      { a: 3, b: 100, c: null },
      { a: 9, b: 20, c: 5 },
      { a: null, b: null, c: 7 },
    ];
    expect(numericDomain(rows, ['a', 'b'])).toEqual([3, 100]);
    expect(numericDomain(rows, ['c'])).toEqual([5, 7]);
  });

  it('returns a zero domain for an empty set', () => {
    expect(numericDomain([], ['a'])).toEqual([0, 0]);
  });
});
