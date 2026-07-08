import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  ResponsiveContainer,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
} from 'recharts';
import { fetchLlmCostDaily, fetchLlmCostHourly } from '@/lib/queries';
import {
  buildDailySeries,
  buildHourlySeries,
  colorTokenFor,
  computeKpis,
  featureLabel,
  summarizeByFeature,
  summarizeByModel,
  type LlmCostDailyRow,
} from '@/lib/llmCosts';
import { fmtCount, fmtRelative, fmtUsd, fmtUsdPerCall } from '@/lib/format';
import { useTokenColors } from '@/lib/useTokenColors';
import GrainToggle, { type Grain } from '@/components/GrainToggle';

/* Operator dashboard for LLM spend — aggregates of the `llm_calls` audit
 * table via `llm_cost_daily_public` (migration 280) and its hour-grain
 * twin `llm_cost_hourly_public` (migration 281). Read-only, anon. */

const CHART_DAYS = 30;
const CHART_HOURS = 48;

const TOKEN_KEYS = [
  '--color-ink-3',
  '--color-rule',
  '--color-paper-2',
  '--color-tag-ochre',
  '--color-tag-slate',
  '--color-tag-brick',
  '--color-tag-teal',
  '--color-tag-plum',
  '--color-tag-sage',
  '--color-tag-sand',
  '--color-tag-copper',
];

const fmtAxisUsd = (v: number): string =>
  v >= 1000 ? `$${Math.round(v / 100) / 10}k` : v >= 10 ? `$${Math.round(v)}` : `$${v}`;

const fmtChartDay = (day: string): string => {
  const d = new Date(`${day}T00:00:00Z`);
  return d.toLocaleDateString('cs-CZ', { day: 'numeric', month: 'numeric' });
};

// Hour buckets are full ISO timestamps; terse ticks, full tooltip — the
// same convention as DedupPipelineTimeline's hour grain.
const fmtChartHour = (iso: string): string =>
  new Date(iso).toLocaleTimeString('cs-CZ', { hour: '2-digit', minute: '2-digit' });

const fmtChartHourFull = (iso: string): string =>
  new Date(iso).toLocaleString('cs-CZ', {
    day: 'numeric', month: 'numeric', hour: '2-digit', minute: '2-digit',
  });

export default function Costs() {
  const { data, isLoading, error, dataUpdatedAt } = useQuery({
    queryKey: ['llm-cost-daily'],
    queryFn: () => fetchLlmCostDaily(35),
    refetchInterval: 5 * 60_000,
  });

  return (
    <div className="px-6 pt-5 pb-8 max-w-screen-2xl mx-auto">
      <div className="flex items-baseline justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl leading-tight">LLM costs</h1>
          <p className="text-sm text-[var(--color-ink-2)] mt-0.5">
            Spend across every LLM call, from the <span className="font-mono">llm_calls</span> audit
            table{dataUpdatedAt ? <> · refreshed {fmtRelative(new Date(dataUpdatedAt).toISOString())}</> : null}
          </p>
        </div>
      </div>

      {error ? (
        <div className="mt-4 rounded-[var(--radius-md)] border border-[var(--color-brick)]/30 bg-[var(--color-brick-soft)] px-4 py-3 text-sm text-[var(--color-brick)]">
          Failed to load cost data: {(error as Error).message}
        </div>
      ) : null}

      {isLoading && !data ? <Skeleton /> : data ? <Body rows={data} /> : null}
    </div>
  );
}

function Body({ rows }: { rows: LlmCostDailyRow[] }) {
  const now = useMemo(() => new Date(), []);
  const [grain, setGrain] = useState<Grain>('day');
  const kpis = useMemo(() => computeKpis(rows, now), [rows, now]);
  const daily = useMemo(() => buildDailySeries(rows, now, CHART_DAYS), [rows, now]);
  const features = useMemo(() => summarizeByFeature(rows, now), [rows, now]);
  const models = useMemo(() => summarizeByModel(rows, now), [rows, now]);

  // Hour grain is fetched lazily, only once the operator flips the toggle.
  const hourlyQuery = useQuery({
    queryKey: ['llm-cost-hourly'],
    queryFn: () => fetchLlmCostHourly(CHART_HOURS + 1),
    enabled: grain === 'hour',
    refetchInterval: 5 * 60_000,
  });
  const hourly = useMemo(
    () => (hourlyQuery.data ? buildHourlySeries(hourlyQuery.data, now, CHART_HOURS) : null),
    [hourlyQuery.data, now],
  );

  return (
    <div className="mt-4 space-y-4">
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
        <Stat label="Today" value={fmtUsd(kpis.today)} />
        <Stat label="Last 7 d" value={fmtUsd(kpis.last7)} />
        <Stat label="Last 30 d" value={fmtUsd(kpis.last30)} />
        <Stat
          label="Projected / month"
          value={fmtUsd(kpis.projectedMonth)}
          hint="7-day average × 30"
          accent
        />
        <Stat label="Calls · 7 d" value={fmtCount(kpis.calls7)} />
        <Stat
          label="Errors · 7 d"
          value={fmtCount(kpis.errors7)}
          danger={kpis.errors7 > 0}
          hint={kpis.errors7 > 0 ? 'failed calls, unbilled' : undefined}
        />
      </div>

      <Card
        title={
          grain === 'hour'
            ? `Hourly spend · last ${CHART_HOURS} h · by feature`
            : `Daily spend · last ${CHART_DAYS} d · by feature`
        }
        accessory={<GrainToggle grain={grain} onChange={setGrain} />}
      >
        {grain === 'hour' ? (
          hourlyQuery.error ? (
            <p className="text-sm text-[var(--color-brick)]">
              Failed to load hourly data: {(hourlyQuery.error as Error).message}
            </p>
          ) : !hourly ? (
            <p className="text-sm text-[var(--color-ink-3)]">Loading hourly spend…</p>
          ) : (
            <CostChart series={hourly} grain="hour" />
          )
        ) : (
          <CostChart series={daily} grain="day" />
        )}
      </Card>

      <Card title="By feature · 7 d / 30 d">
        <FeatureTable features={features} />
      </Card>

      <Card title="By model · last 30 d">
        <ModelTable models={models} />
      </Card>
    </div>
  );
}

function CostChart({
  series,
  grain,
}: {
  series: ReturnType<typeof buildDailySeries>;
  grain: Grain;
}) {
  const colors = useTokenColors(TOKEN_KEYS);
  const axis = colors['--color-ink-3'] || '#7a7d86';
  const grid = colors['--color-rule'] || 'rgba(26,28,34,0.08)';
  const gap = colors['--color-paper-2'] || '#fbf9f3';
  const xKey = grain === 'hour' ? 'bucket' : 'day';
  const fmtTick = grain === 'hour' ? fmtChartHour : fmtChartDay;
  const fmtTooltipLabel = grain === 'hour' ? fmtChartHourFull : fmtChartDay;

  return (
    <div>
      <div className="h-[260px] w-full">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={series.data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid stroke={grid} vertical={false} />
            <XAxis
              dataKey={xKey}
              tickFormatter={fmtTick}
              tick={{ fill: axis, fontSize: 11 }}
              stroke={axis}
              minTickGap={24}
            />
            <YAxis
              tickFormatter={fmtAxisUsd}
              tick={{ fill: axis, fontSize: 11 }}
              stroke={axis}
              width={46}
            />
            <Tooltip
              isAnimationActive={false}
              cursor={{ fill: grid }}
              content={({ active, payload, label }) => {
                if (!active || !payload || !payload.length) return null;
                const visible = payload.filter((p) => (p.value as number) > 0);
                const total = visible.reduce((s, p) => s + (p.value as number), 0);
                return (
                  <div className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-2.5 py-1.5 text-[0.72rem] shadow-sm">
                    <div className="text-[var(--color-ink-3)]">{fmtTooltipLabel(String(label))}</div>
                    {visible.map((p) => (
                      <div key={String(p.dataKey)} className="mt-0.5 flex items-center gap-2 tabular-nums">
                        <span
                          className="inline-block w-2 h-2 rounded-[2px]"
                          style={{ background: p.fill as string }}
                        />
                        <span className="text-[var(--color-ink-3)]">
                          {featureLabel(String(p.dataKey))}
                        </span>
                        <span className="ml-auto pl-3 font-mono text-[var(--color-ink)]">
                          {fmtUsd(p.value as number)}
                        </span>
                      </div>
                    ))}
                    <div className="mt-1 pt-1 border-t border-[var(--color-rule-soft)] flex items-center gap-2 tabular-nums">
                      <span className="text-[var(--color-ink-3)]">Total</span>
                      <span className="ml-auto pl-3 font-mono text-[var(--color-ink)]">{fmtUsd(total)}</span>
                    </div>
                  </div>
                );
              }}
            />
            {series.features.map((f) => (
              <Bar
                key={f}
                dataKey={f}
                stackId="cost"
                fill={colors[colorTokenFor(f)] || '#9c8c5e'}
                stroke={gap}
                strokeWidth={1}
                isAnimationActive={false}
              />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </div>
      {/* Legend: identity never rides on color alone — names always visible. */}
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
        {series.features.map((f) => (
          <span key={f} className="inline-flex items-center gap-1.5 text-[0.72rem] text-[var(--color-ink-2)]">
            <span
              className="inline-block w-2.5 h-2.5 rounded-[2px]"
              style={{ background: colors[colorTokenFor(f)] || '#9c8c5e' }}
            />
            {featureLabel(f)}
          </span>
        ))}
      </div>
    </div>
  );
}

function FeatureTable({ features }: { features: ReturnType<typeof summarizeByFeature> }) {
  const colors = useTokenColors(TOKEN_KEYS);
  if (!features.length) {
    return <p className="text-sm text-[var(--color-ink-3)]">No LLM calls in the last 30 days.</p>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]">
            <th className="py-1.5 pr-3 font-medium">Feature</th>
            <th className="py-1.5 pr-3 font-medium">Model(s)</th>
            <th className="py-1.5 pr-3 font-medium text-right">Calls 7 d</th>
            <th className="py-1.5 pr-3 font-medium text-right">Errors 7 d</th>
            <th className="py-1.5 pr-3 font-medium text-right">Cost 7 d</th>
            <th className="py-1.5 pr-3 font-medium text-right">Avg / call</th>
            <th className="py-1.5 pr-3 font-medium text-right">Cost 30 d</th>
            <th className="py-1.5 font-medium text-right">Share 30 d</th>
          </tr>
        </thead>
        <tbody>
          {features.map((f) => (
            <tr key={f.feature} className="border-t border-[var(--color-rule-soft)]">
              <td className="py-1.5 pr-3">
                <span className="inline-flex items-center gap-2">
                  <span
                    className="inline-block w-2.5 h-2.5 rounded-[2px] shrink-0"
                    style={{ background: colors[colorTokenFor(f.feature)] || '#9c8c5e' }}
                  />
                  {featureLabel(f.feature)}
                </span>
                <span className="block pl-[18px] font-mono text-[0.68rem] text-[var(--color-ink-4)]">
                  {f.feature}
                </span>
              </td>
              <td className="py-1.5 pr-3 font-mono text-[0.72rem] text-[var(--color-ink-2)]">
                {f.models.join(', ')}
              </td>
              <td className="py-1.5 pr-3 text-right font-mono tabular-nums">{fmtCount(f.calls7)}</td>
              <td
                className={`py-1.5 pr-3 text-right font-mono tabular-nums ${
                  f.errors7 > 0 ? 'text-[var(--color-brick)]' : 'text-[var(--color-ink-4)]'
                }`}
              >
                {fmtCount(f.errors7)}
              </td>
              <td className="py-1.5 pr-3 text-right font-mono tabular-nums">{fmtUsd(f.cost7)}</td>
              <td className="py-1.5 pr-3 text-right font-mono tabular-nums text-[var(--color-ink-2)]">
                {fmtUsdPerCall(f.avgPerCall7)}
              </td>
              <td className="py-1.5 pr-3 text-right font-mono tabular-nums">{fmtUsd(f.cost30)}</td>
              <td className="py-1.5 text-right font-mono tabular-nums text-[var(--color-ink-2)]">
                {(f.share30 * 100).toFixed(1)} %
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ModelTable({ models }: { models: ReturnType<typeof summarizeByModel> }) {
  if (!models.length) {
    return <p className="text-sm text-[var(--color-ink-3)]">No LLM calls in the last 30 days.</p>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm max-w-xl">
        <thead>
          <tr className="text-left text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]">
            <th className="py-1.5 pr-3 font-medium">Model</th>
            <th className="py-1.5 pr-3 font-medium">Provider</th>
            <th className="py-1.5 pr-3 font-medium text-right">Calls 30 d</th>
            <th className="py-1.5 pr-3 font-medium text-right">Cost 30 d</th>
            <th className="py-1.5 font-medium text-right">Share</th>
          </tr>
        </thead>
        <tbody>
          {models.map((m) => (
            <tr key={`${m.provider}/${m.model}`} className="border-t border-[var(--color-rule-soft)]">
              <td className="py-1.5 pr-3 font-mono text-[0.78rem]">{m.model}</td>
              <td className="py-1.5 pr-3 text-[var(--color-ink-2)]">{m.provider}</td>
              <td className="py-1.5 pr-3 text-right font-mono tabular-nums">{fmtCount(m.calls30)}</td>
              <td className="py-1.5 pr-3 text-right font-mono tabular-nums">{fmtUsd(m.cost30)}</td>
              <td className="py-1.5 text-right font-mono tabular-nums text-[var(--color-ink-2)]">
                {(m.share30 * 100).toFixed(1)} %
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Card({
  title,
  accessory,
  children,
}: {
  title: string;
  accessory?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
          {title}
        </h3>
        {accessory ?? null}
      </div>
      <div className="mt-3">{children}</div>
    </section>
  );
}

function Stat({
  label,
  value,
  hint,
  accent,
  danger,
}: {
  label: string;
  value: string;
  hint?: string;
  accent?: boolean;
  danger?: boolean;
}) {
  const tone = danger
    ? 'text-[var(--color-brick)]'
    : accent
      ? 'text-[var(--color-copper-2)]'
      : 'text-[var(--color-ink)]';
  return (
    <div className="rounded-[var(--radius-sm)] border border-[var(--color-rule-soft)] bg-[var(--color-paper-2)] px-3 py-2">
      <div className="text-[0.62rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]">{label}</div>
      <div className={`font-mono tabular-nums text-xl ${tone}`}>{value}</div>
      {hint ? <div className="text-[0.68rem] text-[var(--color-ink-4)]">{hint}</div> : null}
    </div>
  );
}

function Skeleton() {
  return (
    <div className="mt-4 space-y-4 animate-pulse">
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="h-16 rounded-[var(--radius-sm)] border border-[var(--color-rule-soft)] bg-[var(--color-paper-2)]" />
        ))}
      </div>
      <div className="h-[320px] rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)]" />
      <div className="h-[240px] rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)]" />
    </div>
  );
}
