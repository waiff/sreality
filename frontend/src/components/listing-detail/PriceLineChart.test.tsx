import { describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/react';
import PriceLineChart from './PriceLineChart';
import type { PriceSeries } from '@/lib/priceHistory';

/* ResponsiveContainer measures its parent, which is 0x0 in jsdom, so the chart
 * body would never render and this file would assert nothing. Giving it a fixed
 * size is what makes the axis, the lines and the per-point dot renderer
 * actually run — this component has crashed the listing page before
 * (recharts #310, "rendered more hooks"). */
vi.mock('recharts', async () => {
  const actual = await vi.importActual<typeof import('recharts')>('recharts');
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: React.ReactElement }) =>
      actual === null ? null : <div>{cloneWithSize(children)}</div>,
  };
});

function cloneWithSize(child: React.ReactElement): React.ReactElement {
  return { ...child, props: { ...child.props, width: 640, height: 230 } };
}

const T = (iso: string) => Date.parse(iso);

const oneTrack: PriceSeries[] = [
  {
    id: 100,
    label: 'Price',
    points: [
      { t: T('2026-06-27T08:00:00Z'), price: 4_000_000 },
      { t: T('2026-06-29T08:00:00Z'), price: 3_700_000 },
    ],
    endT: T('2026-08-03T08:00:00Z'),
  },
];

const axisTexts = (container: HTMLElement, cls: string): string[] =>
  Array.from(container.querySelectorAll(`.${cls} text`)).map((n) => n.textContent ?? '');

describe('PriceLineChart', () => {
  it('dates the axis to the day over a five-week window, with no repeated label', () => {
    const { container } = render(<PriceLineChart series={oneTrack} />);
    const labels = axisTexts(container, 'recharts-xAxis');
    expect(labels.length).toBeGreaterThanOrEqual(4);
    expect(new Set(labels).size).toBe(labels.length);
    for (const l of labels) expect(l).toMatch(/^\d{1,2}\. \d{1,2}\.$/);
  });

  it('labels the price axis distinctly inside a narrow band', () => {
    const { container } = render(<PriceLineChart series={oneTrack} />);
    const labels = axisTexts(container, 'recharts-yAxis');
    expect(labels.length).toBeGreaterThan(1);
    expect(new Set(labels).size).toBe(labels.length);
  });

  it('dots the observations only — not the carried-forward extension to now', () => {
    const { container } = render(<PriceLineChart series={oneTrack} />);
    // Three rows (two snapshots + the live extension), two real observations.
    expect(container.querySelectorAll('.recharts-line-dots circle')).toHaveLength(2);
  });

  it('renders a track per URL without colliding', () => {
    const two: PriceSeries[] = [
      oneTrack[0],
      {
        id: 200,
        label: 'Bazos',
        points: [{ t: T('2026-07-10T08:00:00Z'), price: 3_950_000 }],
        endT: T('2026-08-03T08:00:00Z'),
      },
    ];
    const { container } = render(<PriceLineChart series={two} />);
    expect(container.querySelectorAll('.recharts-line')).toHaveLength(2);
    // The second track is dotted once, the first twice — its window starts late.
    expect(container.querySelectorAll('.recharts-line-dots circle')).toHaveLength(3);
  });

  it('breaks the line where activeWindows leaves a gap, and does not bridge it', () => {
    // A snapshot at 2026-07-17 lands inside the dark stretch between the two
    // windows, so it's the row that actually forces a null in the middle.
    const track: PriceSeries[] = [
      {
        id: 1,
        label: 'Price',
        points: [
          { t: T('2026-06-27T08:00:00Z'), price: 4_000_000 },
          { t: T('2026-07-17T08:00:00Z'), price: 3_900_000 },
        ],
        endT: T('2026-08-03T08:00:00Z'),
      },
    ];
    const activeWindows: [number, number][] = [
      [T('2026-06-27T08:00:00Z'), T('2026-07-10T08:00:00Z')],
      [T('2026-07-25T08:00:00Z'), T('2026-08-03T08:00:00Z')],
    ];
    const { container } = render(
      <PriceLineChart series={track} activeWindows={activeWindows} />,
    );
    // recharts draws one SVG <path> per Line even with an internal null run,
    // but (without connectNulls) the path `d` gets a second "M" (moveto) —
    // one subpath per contiguous non-null stretch instead of one continuous
    // curve bridging the gap.
    const d = container.querySelector('.recharts-line-curve')?.getAttribute('d') ?? '';
    expect(d.match(/M/g)?.length).toBe(2);
  });

  it('survives a single-observation track (no span to scale)', () => {
    const flat: PriceSeries[] = [
      {
        id: 1,
        label: 'Price',
        points: [{ t: T('2026-07-10T08:00:00Z'), price: 2_450_000 }],
        endT: T('2026-07-10T08:00:00Z'),
      },
    ];
    expect(() => render(<PriceLineChart series={flat} />)).not.toThrow();
  });
});
