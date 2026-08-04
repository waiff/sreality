import type { ReactElement } from 'react';
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
import { useTokenColors } from '@/lib/useTokenColors';
import { timeAxisSpec, valueAxisSpec } from '@/lib/chartAxis';
import {
  buildChartRows,
  priceChangeEvents,
  seriesObservedKey,
  seriesValueKey,
  type PriceSeries,
} from '@/lib/priceHistory';

/* One price track = one URL/listing under the property (see lib/priceHistory).
 * Lazy-loaded so recharts stays out of the detail-page entry chunk.
 *
 * Ticks and labels come from lib/chartAxis, shared with every other time series
 * in the app: calendar-aligned steps whose label granularity follows the span,
 * so a five-week window is dated to the day instead of printing "červenec 26"
 * twice. */

// Palette mirrors the civic-archive tokens; primary track = copper.
const PALETTE = ['--color-copper', '--color-brick', '--color-sage', '--color-ink-2'];
const TOKEN_KEYS = ['--color-ink-3', '--color-rule', '--color-paper-2', ...PALETTE];

interface DotProps {
  cx?: number;
  cy?: number;
  payload?: Record<string, unknown>;
}

export default function PriceLineChart({ series }: { series: PriceSeries[] }) {
  const colors = useTokenColors(TOKEN_KEYS);

  const data = buildChartRows(series);
  const times = data.map((row) => row.t as number);
  const prices = series.flatMap((s) => s.points.map((p) => p.price));
  const domain: [number, number] = [times[0] ?? 0, times[times.length - 1] ?? 0];
  const axisSpec = timeAxisSpec(domain, { targetTicks: 6 });
  const valueSpec = valueAxisSpec(
    prices.length ? [Math.min(...prices), Math.max(...prices)] : [0, 0],
    { targetTicks: 5 },
  );

  // (track, instant) -> the step that landed there: drives the emphasised dot
  // and the delta in the tooltip.
  const changes = new Map(priceChangeEvents(series).map((e) => [`${e.seriesId}|${e.t}`, e]));

  const axis = colors['--color-ink-3'] || '#7a7d86';
  const grid = colors['--color-rule'] || 'rgba(26,28,34,0.08)';
  const paper = colors['--color-paper-2'] || '#fbf9f3';

  return (
    <div className="h-[230px] w-full">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 14, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={grid} vertical={false} />
          <XAxis
            dataKey="t"
            type="number"
            scale="time"
            domain={domain}
            ticks={axisSpec.ticks}
            tickFormatter={axisSpec.formatTick}
            tick={{ fill: axis, fontSize: 11 }}
            stroke={axis}
            minTickGap={24}
          />
          <YAxis
            domain={valueSpec.domain}
            ticks={valueSpec.ticks}
            tickFormatter={valueSpec.format}
            tick={{ fill: axis, fontSize: 11 }}
            stroke={axis}
            width={62}
          />
          <Tooltip
            isAnimationActive={false}
            content={({ active, payload, label }) =>
              active && payload && payload.length ? (
                <div className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-2.5 py-1.5 text-[0.72rem] shadow-sm">
                  <div className="text-[var(--color-ink-3)]">
                    {axisSpec.formatFull(label as number)}
                  </div>
                  {payload
                    .filter((p) => p.value != null)
                    .map((p) => {
                      const s = series.find((x) => seriesValueKey(x.id) === p.dataKey);
                      const change = s ? changes.get(`${s.id}|${label as number}`) : undefined;
                      return (
                        <div
                          key={String(p.dataKey)}
                          className="mt-0.5 flex items-center gap-2 tabular-nums"
                        >
                          <span
                            className="inline-block w-2 h-2 rounded-full"
                            style={{ background: p.stroke as string }}
                          />
                          <span className="text-[var(--color-ink-3)]">{s?.label}</span>
                          <span className="ml-auto font-mono text-[var(--color-ink)]">
                            {fmtCzk(p.value as number)}
                          </span>
                          {change ? (
                            <span
                              className="font-mono"
                              style={{
                                color:
                                  change.pct < 0 ? 'var(--color-sage)' : 'var(--color-brick)',
                              }}
                            >
                              {change.pct > 0 ? '+' : '−'}
                              {Math.abs(change.pct).toFixed(1).replace('.', ',')}&nbsp;%
                            </span>
                          ) : null}
                        </div>
                      );
                    })}
                </div>
              ) : null
            }
          />
          {series.map((s, i) => {
            const stroke = colors[PALETTE[i % PALETTE.length]] || '#3c6e63';
            // A dot means "observed here". Rows carry every track's timestamps
            // plus the live extension to now, so dotting all of them would
            // invent observations this URL never had.
            const renderDot = ({ cx, cy, payload }: DotProps): ReactElement => {
              const key = `${s.id}-${String(payload?.t)}`;
              if (cx == null || cy == null || !payload?.[seriesObservedKey(s.id)]) {
                return <g key={key} />;
              }
              const isChange = changes.has(`${s.id}|${payload.t as number}`);
              return (
                <circle
                  key={key}
                  cx={cx}
                  cy={cy}
                  r={isChange ? 3.6 : 2.2}
                  fill={isChange ? stroke : paper}
                  stroke={stroke}
                  strokeWidth={1.4}
                />
              );
            };
            return (
              <Line
                key={s.id}
                type="stepAfter"
                dataKey={seriesValueKey(s.id)}
                name={s.label}
                stroke={stroke}
                strokeWidth={1.6}
                dot={renderDot}
                activeDot={{ r: 4 }}
                connectNulls
                isAnimationActive={false}
              />
            );
          })}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
