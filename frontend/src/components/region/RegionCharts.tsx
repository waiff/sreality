import { useMemo } from 'react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { ActiveByDayRow } from '@/lib/types';

const COPPER = '#3c6e63';
const RULE = 'rgba(26, 28, 34, 0.16)';
const INK_3 = '#7a7d86';

const csDay = new Intl.DateTimeFormat('cs-CZ', { day: '2-digit', month: '2-digit' });
const csMonth = new Intl.DateTimeFormat('cs-CZ', { month: 'short' });

interface Props {
  data: ActiveByDayRow[];
}

/* Active per day, last 90 days. Single copper line, no fill, no gridlines.
 * Subtitle is rendered by the parent. */
export function ActiveByDayChart({ data }: Props) {
  const series = useMemo(
    () => data.map((r) => ({ ...r, dayMs: new Date(r.day).getTime() })),
    [data],
  );

  if (series.length === 0) {
    return <ChartFallback />;
  }

  const monthTicks = useMemo(() => {
    const seen = new Set<string>();
    const out: number[] = [];
    for (const r of series) {
      const key = csMonth.format(new Date(r.dayMs));
      if (!seen.has(key)) {
        seen.add(key);
        out.push(r.dayMs);
      }
    }
    return out;
  }, [series]);

  return (
    <div className="h-[260px] w-full">
      <ResponsiveContainer>
        <LineChart data={series} margin={{ top: 8, right: 4, bottom: 4, left: 4 }}>
          <CartesianGrid stroke="transparent" vertical={false} />
          <XAxis
            dataKey="dayMs"
            type="number"
            scale="time"
            domain={['dataMin', 'dataMax']}
            ticks={monthTicks}
            tickFormatter={(v: number) => csMonth.format(new Date(v))}
            stroke={RULE}
            tick={{ fill: INK_3, fontSize: 11, fontFamily: 'JetBrains Mono, ui-monospace, monospace' }}
            axisLine={{ stroke: RULE }}
            tickLine={{ stroke: RULE }}
            tickMargin={6}
          />
          <YAxis
            dataKey="active"
            stroke={RULE}
            tick={{ fill: INK_3, fontSize: 11, fontFamily: 'JetBrains Mono, ui-monospace, monospace' }}
            axisLine={false}
            tickLine={false}
            width={36}
            tickCount={3}
            allowDecimals={false}
          />
          <Tooltip
            cursor={{ stroke: RULE, strokeWidth: 1 }}
            contentStyle={{
              background: '#ffffff',
              border: `1px solid ${RULE}`,
              borderRadius: 6,
              fontFamily: 'Inter, ui-sans-serif, sans-serif',
              fontSize: 12,
              padding: '6px 10px',
            }}
            labelFormatter={(v) => csDay.format(new Date(Number(v)))}
            formatter={(value: number) => [value.toLocaleString('cs-CZ'), 'active']}
          />
          <Line
            type="monotone"
            dataKey="active"
            stroke={COPPER}
            strokeWidth={1.25}
            dot={false}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

/* New listings per ISO week, last 12 weeks. Bars are copper, no card chrome. */
export function NewByWeekChart({ data }: Props) {
  const weeks = useMemo(() => rollupWeeks(data, 12), [data]);

  if (weeks.length === 0) {
    return <ChartFallback />;
  }

  return (
    <div className="h-[180px] w-full">
      <ResponsiveContainer>
        <BarChart data={weeks} margin={{ top: 8, right: 4, bottom: 4, left: 4 }}>
          <CartesianGrid stroke="transparent" vertical={false} />
          <XAxis
            dataKey="label"
            stroke={RULE}
            tick={{ fill: INK_3, fontSize: 11, fontFamily: 'JetBrains Mono, ui-monospace, monospace' }}
            axisLine={{ stroke: RULE }}
            tickLine={false}
            interval={1}
            tickMargin={6}
          />
          <YAxis
            dataKey="n"
            stroke={RULE}
            tick={{ fill: INK_3, fontSize: 11, fontFamily: 'JetBrains Mono, ui-monospace, monospace' }}
            axisLine={false}
            tickLine={false}
            width={36}
            tickCount={3}
            allowDecimals={false}
          />
          <Tooltip
            cursor={{ fill: 'rgba(60, 110, 99, 0.06)' }}
            contentStyle={{
              background: '#ffffff',
              border: `1px solid ${RULE}`,
              borderRadius: 6,
              fontFamily: 'Inter, ui-sans-serif, sans-serif',
              fontSize: 12,
              padding: '6px 10px',
            }}
            labelFormatter={(label) => `Week ${label}`}
            formatter={(value: number) => [value.toLocaleString('cs-CZ'), 'new']}
          />
          <Bar dataKey="n" fill={COPPER} radius={[1, 1, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

interface WeekBucket {
  key: string;
  label: string;
  n: number;
}

const isoWeekStart = (d: Date): Date => {
  const out = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
  const dow = (out.getUTCDay() + 6) % 7; // Mon=0..Sun=6
  out.setUTCDate(out.getUTCDate() - dow);
  return out;
};

const isoWeekLabel = (d: Date): string => {
  const target = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
  const dayNr = (target.getUTCDay() + 6) % 7;
  target.setUTCDate(target.getUTCDate() - dayNr + 3);
  const firstThu = new Date(Date.UTC(target.getUTCFullYear(), 0, 4));
  const week =
    1 +
    Math.round(
      ((target.getTime() - firstThu.getTime()) / 86_400_000 -
        3 +
        ((firstThu.getUTCDay() + 6) % 7)) /
        7,
    );
  return `W${String(week).padStart(2, '0')}`;
};

const rollupWeeks = (data: ActiveByDayRow[], n: number): WeekBucket[] => {
  if (data.length === 0) return [];
  const map = new Map<string, WeekBucket>();
  for (const row of data) {
    const ws = isoWeekStart(new Date(row.day));
    const key = ws.toISOString().slice(0, 10);
    const existing = map.get(key);
    if (existing) existing.n += row.new;
    else map.set(key, { key, label: isoWeekLabel(ws), n: row.new });
  }
  const sorted = [...map.values()].sort((a, b) => (a.key < b.key ? -1 : 1));
  return sorted.slice(-n);
};

function ChartFallback() {
  return (
    <p className="text-sm text-[var(--color-ink-3)] italic">No data in window.</p>
  );
}
