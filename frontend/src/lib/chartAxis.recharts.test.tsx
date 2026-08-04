import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { CartesianGrid, LineChart, Line, XAxis, YAxis } from 'recharts';
import { timeAxisSpec, valueAxisSpec } from './chartAxis';

/* Contract test against recharts itself, not against our maths (chartAxis.test
 * covers that): an explicit `ticks` array on a numeric/time XAxis must be the
 * axis that renders. Recharts only honours it through an internal branch
 * (getTicksOfAxis -> axis.ticks), so a version bump that dropped it would
 * silently hand the axis back to the evenly-spaced-milliseconds behaviour this
 * module exists to replace. A fixed-size chart is used because
 * ResponsiveContainer measures to 0 in jsdom. */

function renderChart(from: string, to: string) {
  const domain: [number, number] = [Date.parse(from), Date.parse(to)];
  const spec = timeAxisSpec(domain, { targetTicks: 6 });
  const data = [
    { t: domain[0], v: 4_000_000 },
    { t: domain[1], v: 3_700_000 },
  ];
  const valueSpec = valueAxisSpec([3_700_000, 4_000_000]);
  const view = render(
    <LineChart width={640} height={240} data={data}>
      <CartesianGrid />
      <XAxis
        dataKey="t"
        type="number"
        scale="time"
        domain={domain}
        ticks={spec.ticks}
        tickFormatter={spec.formatTick}
      />
      <YAxis
        domain={valueSpec.domain}
        ticks={valueSpec.ticks}
        tickFormatter={valueSpec.format}
      />
      <Line dataKey="v" isAnimationActive={false} />
    </LineChart>,
  );
  return { view, spec };
}

const axisTexts = (container: HTMLElement, cls: string): string[] =>
  Array.from(container.querySelectorAll(`.${cls} text`)).map((n) => n.textContent ?? '');

describe('recharts honours the chartAxis ticks', () => {
  it('renders the calendar ticks for the five-week window from the bug report', () => {
    const { view, spec } = renderChart('2026-06-27T14:32:00Z', '2026-08-03T09:10:00Z');
    const rendered = axisTexts(view.container, 'recharts-xAxis');
    expect(rendered.length).toBeGreaterThanOrEqual(4);
    // Every rendered label is one of ours, dated to the day, and unique.
    const expected = spec.ticks.map(spec.formatTick);
    for (const label of rendered) expect(expected).toContain(label);
    expect(new Set(rendered).size).toBe(rendered.length);
    for (const label of rendered) expect(label).toMatch(/^\d{1,2}\. \d{1,2}\.$/);
  });

  it('no longer prints the same month twice', () => {
    const { view } = renderChart('2026-06-27T14:32:00Z', '2026-08-03T09:10:00Z');
    const rendered = axisTexts(view.container, 'recharts-xAxis');
    expect(rendered).not.toContain('červenec 26');
  });

  it('keeps the price axis labels distinct in a narrow band', () => {
    const { view } = renderChart('2026-06-27T14:32:00Z', '2026-08-03T09:10:00Z');
    const rendered = axisTexts(view.container, 'recharts-yAxis');
    expect(rendered.length).toBeGreaterThan(1);
    expect(new Set(rendered).size).toBe(rendered.length);
  });
});
