import { useQuery } from '@tanstack/react-query';
import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
} from 'recharts';

import { getDedupPipelineTimeline } from '@/lib/api';
import { fmtCount } from '@/lib/format';
import { useTokenColors } from '@/lib/useTokenColors';

/* How the funnel evolves day by day. Two Y-axes because the scales differ by orders of
 * magnitude: CLIP tagging is tens/hundreds of thousands a day (right, copper — the input
 * flow), while candidates + decisions are tens/hundreds (left). One timeline, so the
 * operator can see the whole machine moving. Civic-archive tokens, lazy recharts. */

const TOKEN_KEYS = [
  '--color-ink-3',
  '--color-rule',
  '--color-copper',
  '--color-sage',
  '--color-brick',
  '--color-ink-2',
] as const;

const SERIES = [
  { key: 'tagged', label: 'Tagged', token: '--color-copper', axis: 'right' as const },
  { key: 'candidates', label: 'Candidates', token: '--color-ink-2', axis: 'left' as const },
  { key: 'merged', label: 'Merged', token: '--color-sage', axis: 'left' as const },
  { key: 'dismissed', label: 'Dismissed', token: '--color-brick', axis: 'left' as const },
];

function fmtDay(d: string): string {
  // d is 'YYYY-MM-DD'; render as cs-CZ day.month
  const [y, m, dd] = d.split('-').map(Number);
  return new Date(y, m - 1, dd).toLocaleDateString('cs-CZ', {
    day: 'numeric',
    month: 'numeric',
  });
}

function fmtAxis(v: number): string {
  if (v >= 1000) return `${Math.round(v / 1000)}k`;
  return String(v);
}

export default function DedupPipelineTimeline() {
  const colors = useTokenColors(TOKEN_KEYS);
  const q = useQuery({
    queryKey: ['dedup', 'pipeline-timeline', 14],
    queryFn: () => getDedupPipelineTimeline(14),
    staleTime: 5 * 60_000,
  });
  const data = q.data?.data ?? [];
  const axis = colors['--color-ink-3'] || '#7a7d86';
  const grid = colors['--color-rule'] || 'rgba(26,28,34,0.08)';

  return (
    <div className="mt-3 pt-3 border-t border-[var(--color-rule)]">
      <div className="flex items-center justify-between gap-3 mb-2">
        <p className="text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
          Last 14 days
        </p>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          {SERIES.map((s) => (
            <span key={s.key} className="inline-flex items-center gap-1.5 text-[0.7rem] text-[var(--color-ink-3)]">
              <span
                className="inline-block h-2 w-2 rounded-full"
                style={{ background: `var(${s.token})` }}
              />
              {s.label}
            </span>
          ))}
        </div>
      </div>
      {q.isLoading ? (
        <p className="text-sm text-[var(--color-ink-3)]">Loading…</p>
      ) : data.length === 0 ? (
        <p className="text-sm text-[var(--color-ink-3)]">No activity yet.</p>
      ) : (
        <div className="h-[200px] w-full">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid stroke={grid} vertical={false} />
              <XAxis
                dataKey="day"
                tickFormatter={fmtDay}
                tick={{ fill: axis, fontSize: 11 }}
                stroke={axis}
                minTickGap={24}
              />
              <YAxis
                yAxisId="left"
                tickFormatter={fmtAxis}
                tick={{ fill: axis, fontSize: 11 }}
                stroke={axis}
                width={40}
                allowDecimals={false}
              />
              <YAxis
                yAxisId="right"
                orientation="right"
                tickFormatter={fmtAxis}
                tick={{ fill: axis, fontSize: 11 }}
                stroke={axis}
                width={44}
                allowDecimals={false}
              />
              <Tooltip
                isAnimationActive={false}
                content={({ active, payload, label }) =>
                  active && payload && payload.length ? (
                    <div className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-2.5 py-1.5 text-[0.72rem] shadow-sm">
                      <div className="text-[var(--color-ink-3)] mb-0.5">{fmtDay(label as string)}</div>
                      {SERIES.map((s) => {
                        const p = payload.find((x) => x.dataKey === s.key);
                        return (
                          <div key={s.key} className="flex items-center gap-2">
                            <span
                              className="inline-block h-2 w-2 rounded-full"
                              style={{ background: `var(${s.token})` }}
                            />
                            <span className="text-[var(--color-ink-3)]">{s.label}</span>
                            <span className="ml-auto font-mono tabular-nums text-[var(--color-ink)]">
                              {fmtCount(Number(p?.value ?? 0))}
                            </span>
                          </div>
                        );
                      })}
                    </div>
                  ) : null
                }
              />
              {SERIES.map((s) => (
                <Line
                  key={s.key}
                  yAxisId={s.axis}
                  type="monotone"
                  dataKey={s.key}
                  stroke={colors[s.token] || '#3c6e63'}
                  strokeWidth={s.key === 'tagged' ? 2 : 1.5}
                  dot={false}
                  isAnimationActive={false}
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}
