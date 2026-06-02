import { useEffect, useState } from 'react';
import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
} from 'recharts';
import { fmtCzk } from '@/lib/format';
import type { PriceSeries } from '@/lib/priceHistory';

/* One price track = one URL/listing under the property (see lib/priceHistory).
 * Lazy-loaded so recharts stays out of the detail-page entry chunk. */

// Palette mirrors the civic-archive tokens; primary track = copper.
const PALETTE = ['--color-copper', '--color-brick', '--color-sage', '--color-ink-2'];
const TOKEN_KEYS = ['--color-ink-3', '--color-rule', ...PALETTE];

function readColors(keys: string[]): Record<string, string> {
  if (typeof window === 'undefined') return {};
  const cs = getComputedStyle(document.documentElement);
  const out: Record<string, string> = {};
  for (const k of keys) out[k] = cs.getPropertyValue(k).trim();
  return out;
}

/* Resolve the design tokens to concrete colours (recharts strokes are SVG
 * presentation attributes, where `var()` doesn't resolve) and re-read on a
 * light/dark switch — explicit (`data-theme`) or system (prefers-color-scheme). */
function useTokenColors(): Record<string, string> {
  const [colors, setColors] = useState(() => readColors(TOKEN_KEYS));
  useEffect(() => {
    const update = () => setColors(readColors(TOKEN_KEYS));
    update();
    const obs = new MutationObserver(update);
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    mq.addEventListener('change', update);
    return () => {
      obs.disconnect();
      mq.removeEventListener('change', update);
    };
  }, []);
  return colors;
}

function fmtAxisCzk(v: number): string {
  if (v >= 1_000_000) {
    return `${(v / 1_000_000).toLocaleString('cs-CZ', { maximumFractionDigits: 1 })} M`;
  }
  if (v >= 1_000) return `${Math.round(v / 1_000)} k`;
  return String(v);
}

function fmtAxisDate(t: number): string {
  return new Date(t).toLocaleDateString('cs-CZ', { month: 'short', year: '2-digit' });
}

function fmtFullDate(t: number): string {
  return new Date(t).toLocaleDateString('cs-CZ', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
  });
}

export default function PriceLineChart({ series }: { series: PriceSeries[] }) {
  const colors = useTokenColors();

  // Merge every track onto one sorted time axis, carrying the last known price
  // forward (the step) and leaving NULL outside a track's [start, endT] window.
  const times = new Set<number>();
  for (const s of series) {
    for (const p of s.points) times.add(p.t);
    times.add(s.endT);
  }
  const data = [...times]
    .sort((a, b) => a - b)
    .map((t) => {
      const row: Record<string, number | null> = { t };
      for (const s of series) {
        if (!s.points.length || t < s.points[0].t || t > s.endT) {
          row[`s${s.id}`] = null;
          continue;
        }
        let v = s.points[0].price;
        for (const p of s.points) {
          if (p.t <= t) v = p.price;
          else break;
        }
        row[`s${s.id}`] = v;
      }
      return row;
    });

  const axis = colors['--color-ink-3'] || '#7a7d86';
  const grid = colors['--color-rule'] || 'rgba(26,28,34,0.08)';

  return (
    <div className="h-[230px] w-full">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 14, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={grid} vertical={false} />
          <XAxis
            dataKey="t"
            type="number"
            domain={['dataMin', 'dataMax']}
            tickFormatter={fmtAxisDate}
            tick={{ fill: axis, fontSize: 11 }}
            stroke={axis}
            minTickGap={36}
          />
          <YAxis
            tickFormatter={fmtAxisCzk}
            tick={{ fill: axis, fontSize: 11 }}
            stroke={axis}
            width={52}
            domain={['auto', 'auto']}
          />
          <Tooltip
            isAnimationActive={false}
            content={({ active, payload, label }) =>
              active && payload && payload.length ? (
                <div className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-2.5 py-1.5 text-[0.72rem] shadow-sm">
                  <div className="text-[var(--color-ink-3)]">{fmtFullDate(label as number)}</div>
                  {payload
                    .filter((p) => p.value != null)
                    .map((p) => {
                      const s = series.find((x) => `s${x.id}` === p.dataKey);
                      return (
                        <div key={String(p.dataKey)} className="mt-0.5 flex items-center gap-2 tabular-nums">
                          <span
                            className="inline-block w-2 h-2 rounded-full"
                            style={{ background: p.stroke as string }}
                          />
                          <span className="text-[var(--color-ink-3)]">{s?.label}</span>
                          <span className="ml-auto font-mono text-[var(--color-ink)]">
                            {fmtCzk(p.value as number)}
                          </span>
                        </div>
                      );
                    })}
                </div>
              ) : null
            }
          />
          {series.map((s, i) => (
            <Line
              key={s.id}
              type="stepAfter"
              dataKey={`s${s.id}`}
              name={s.label}
              stroke={colors[PALETTE[i % PALETTE.length]] || '#3c6e63'}
              strokeWidth={1.6}
              dot={{ r: 2.5 }}
              activeDot={{ r: 4 }}
              connectNulls
              isAnimationActive={false}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
