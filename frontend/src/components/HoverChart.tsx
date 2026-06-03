/* Small fixed-axis line chart shown when hovering an obec on a growth map.
 * The domains (x = months, y = value) are passed in fixed across all obce, so
 * the line reads as one chart with a moving trace as the cursor moves between
 * municipalities. X labels are years (not month names). Civic-archive tokens. */
import type { HoverPoint } from '@/lib/growthChoropleth';

const W = 248;
const H = 156;
const PAD_L = 6;
const PAD_R = 6;
const PAD_T = 24;
const PAD_B = 18;
const PLOT_W = W - PAD_L - PAD_R;
const PLOT_H = H - PAD_T - PAD_B;

interface Props {
  title: string;
  points: HoverPoint[];
  xMin: number;
  xMax: number;
  yMin: number;
  yMax: number;
  valueLabel: string;
  format: (v: number) => string;
}

export default function HoverChart({
  title, points, xMin, xMax, yMin, yMax, valueLabel, format,
}: Props) {
  const xSpan = Math.max(1, xMax - xMin);
  const ySpan = Math.max(1e-9, yMax - yMin);
  const sx = (ymi: number) => PAD_L + ((ymi - xMin) / xSpan) * PLOT_W;
  const sy = (v: number) => PAD_T + (1 - (v - yMin) / ySpan) * PLOT_H;

  const line = points.map((p) => `${sx(p.ymi).toFixed(1)},${sy(p.value).toFixed(1)}`).join(' ');
  const latest = points.length ? points[points.length - 1].value : null;

  const firstYear = Math.ceil(xMin / 12);
  const lastYear = Math.floor(xMax / 12);
  const span = lastYear - firstYear;
  const step = span <= 5 ? 1 : span <= 11 ? 2 : 3;
  const years: number[] = [];
  for (let y = firstYear; y <= lastYear; y += step) years.push(y);

  return (
    <div className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-3)]/97 shadow-[0_2px_8px_rgba(0,0,0,0.08)] px-2 pt-1.5 pb-1">
      <div className="flex items-baseline justify-between gap-3 px-1">
        <span className="text-[0.72rem] text-[var(--color-ink)] truncate max-w-[150px]">{title}</span>
        <span className="text-[0.72rem] tabular-nums text-[var(--color-copper)]">
          {latest != null ? format(latest) : '—'}
        </span>
      </div>
      <svg width={W} height={H} className="block">
        {/* y bounds */}
        <line x1={PAD_L} y1={PAD_T} x2={PAD_L} y2={PAD_T + PLOT_H} style={{ stroke: 'var(--color-rule)' }} strokeWidth={1} />
        <line x1={PAD_L} y1={PAD_T + PLOT_H} x2={PAD_L + PLOT_W} y2={PAD_T + PLOT_H} style={{ stroke: 'var(--color-rule)' }} strokeWidth={1} />
        <text x={PAD_L + 2} y={PAD_T + 8} style={{ fill: 'var(--color-ink-3)' }} fontSize={8}>{format(yMax)}</text>
        <text x={PAD_L + 2} y={PAD_T + PLOT_H - 2} style={{ fill: 'var(--color-ink-3)' }} fontSize={8}>{format(yMin)}</text>
        {/* year gridlines + labels */}
        {years.map((y) => {
          const x = sx(y * 12);
          if (x < PAD_L || x > PAD_L + PLOT_W) return null;
          return (
            <g key={y}>
              <line x1={x} y1={PAD_T} x2={x} y2={PAD_T + PLOT_H} style={{ stroke: 'var(--color-rule-soft)' }} strokeWidth={1} />
              <text x={x} y={H - 5} textAnchor="middle" style={{ fill: 'var(--color-ink-3)' }} fontSize={8}>{y}</text>
            </g>
          );
        })}
        {/* the trace */}
        {points.length > 1 && (
          <polyline points={line} fill="none" style={{ stroke: 'var(--color-copper)' }} strokeWidth={1.5} strokeLinejoin="round" />
        )}
        {points.length === 1 && (
          <circle cx={sx(points[0].ymi)} cy={sy(points[0].value)} r={2} style={{ fill: 'var(--color-copper)' }} />
        )}
      </svg>
      <div className="px-1 text-[0.6rem] text-[var(--color-ink-4)]">{valueLabel}</div>
    </div>
  );
}
