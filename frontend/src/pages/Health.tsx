import { useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import {
  fetchHealthSummary,
  fetchImageStorageOverview,
  fetchRecentScrapeRuns,
} from '@/lib/queries';
import type {
  HealthSummary,
  HealthDayCount,
  HealthSnapBucket,
  HealthFreshnessRow,
  HealthFailureRow,
  HealthCategoryBlock,
  ImageStorageCategory,
  ImageStorageOverview,
  ScrapeRun,
  ScrapeRunCategory,
} from '@/lib/types';
import { fmtCount, fmtRelative, fmtAbsolute } from '@/lib/format';

const STALE_HOURS_WARN = 36;

const CATEGORY_LABELS: Record<string, string> = {
  byt: 'Byty',
  dum: 'Domy',
  komercni: 'Komerční',
};

const TYPE_LABELS: Record<string, string> = {
  pronajem: 'pronájem',
  prodej: 'prodej',
};

function categoryLabel(c: HealthCategoryBlock): string {
  const main = CATEGORY_LABELS[c.category_main] ?? c.category_main;
  const type = TYPE_LABELS[c.category_type] ?? c.category_type;
  return `${main} · ${type}`;
}

export default function Health() {
  const { data, isLoading, error, dataUpdatedAt } = useQuery<HealthSummary, Error>({
    queryKey: ['health-summary'],
    queryFn: fetchHealthSummary,
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  return (
    <div className="px-6 pt-5 pb-8 max-w-screen-2xl mx-auto">
      <header className="flex items-baseline justify-between gap-4">
        <div>
          <h1 className="text-2xl leading-tight">Health</h1>
          <p className="mt-1 text-sm text-[var(--color-ink-2)]">
            Scraper status by category, snapshot density, fetch failures.{' '}
            {dataUpdatedAt > 0 && (
              <span className="text-[var(--color-ink-3)]">
                · refreshed {fmtRelative(new Date(dataUpdatedAt).toISOString())}
              </span>
            )}
          </p>
        </div>
      </header>

      {error && (
        <div className="mt-4 p-3 rounded-[var(--radius-sm)] border border-[var(--color-brick)]/30 bg-[var(--color-brick-soft)] text-sm text-[var(--color-brick)]">
          <strong className="font-medium">health_summary failed:</strong> {error.message}
        </div>
      )}

      {data && <StaleScrapeBanner lastScrapeAt={data.last_scrape_at} />}

      {isLoading && !data ? (
        <Skeleton />
      ) : data ? (
        <Body data={data} />
      ) : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function Body({ data }: { data: HealthSummary }) {
  const scrapeRunsQuery = useQuery<ScrapeRun[], Error>({
    queryKey: ['scrape-runs', 14],
    queryFn: () => fetchRecentScrapeRuns(14),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
  const imageOverviewQuery = useQuery<ImageStorageOverview, Error>({
    queryKey: ['image-storage-overview'],
    queryFn: fetchImageStorageOverview,
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  return (
    <div className="mt-5 space-y-5">
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <LastScrapeTile lastScrapeAt={data.last_scrape_at} />
        {data.by_category.map((c) => (
          <CategoryTile key={`${c.category_main}-${c.category_type}`} block={c} />
        ))}
      </div>

      <Card label="Recent scrapes · last 14 d">
        <RecentScrapesPanel
          rows={scrapeRunsQuery.data}
          isLoading={scrapeRunsQuery.isLoading}
          error={scrapeRunsQuery.error}
        />
      </Card>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Card label="Image mirror">
          <ImageMirrorPanel
            overview={imageOverviewQuery.data}
            isLoading={imageOverviewQuery.isLoading}
            error={imageOverviewQuery.error}
          />
        </Card>
        <Card label="Schedule">
          <SchedulePanel />
        </Card>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Card label="Snapshot density">
          <SnapshotBars rows={data.snapshot_density} totalListings={data.active_now} />
        </Card>
        <Card label="Freshness checks · last 24 h">
          <FreshnessRows rows={data.freshness_24h} />
        </Card>
      </div>

      <Card label="Fetch failures · top 10 by attempts">
        <FailuresPanel
          given_up={data.failures_given_up}
          total={data.failures_total}
          top10={data.failures_top10}
        />
      </Card>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Stale-scrape warning                                                       */
/* -------------------------------------------------------------------------- */

function StaleScrapeBanner({ lastScrapeAt }: { lastScrapeAt: string | null }) {
  if (!lastScrapeAt) return null;
  const ageH = (Date.now() - new Date(lastScrapeAt).getTime()) / 3_600_000;
  if (ageH < STALE_HOURS_WARN) return null;
  return (
    <div className="mt-4 p-3 rounded-[var(--radius-sm)] border border-[var(--color-ochre)]/40 bg-[var(--color-ochre-soft)] text-sm text-[var(--color-ochre)] flex items-baseline gap-2">
      <span className="text-[0.7rem] tracking-[0.18em] uppercase font-medium">stale</span>
      <span>
        No scrape activity in <span className="font-mono tabular-nums">{Math.round(ageH)}&thinsp;h</span>.
        The daily cron may have failed — check the latest run in GitHub Actions.
      </span>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Last-scrape global tile (keeps a single global anchor in the grid)         */
/* -------------------------------------------------------------------------- */

function LastScrapeTile({ lastScrapeAt }: { lastScrapeAt: string | null }) {
  return (
    <div className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4">
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
        Last scrape run
      </p>
      <p
        className="mt-2 font-display text-[2rem] leading-none tracking-tight text-[var(--color-ink)]"
        title={lastScrapeAt ? fmtAbsolute(lastScrapeAt) : undefined}
      >
        {lastScrapeAt ? fmtRelative(lastScrapeAt) : '—'}
      </p>
      {lastScrapeAt && (
        <p className="mt-1 text-[0.65rem] text-[var(--color-ink-4)]">{fmtAbsolute(lastScrapeAt)}</p>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Per-category tile                                                          */
/* -------------------------------------------------------------------------- */

function CategoryTile({ block }: { block: HealthCategoryBlock }) {
  const newTotal = block.new_per_day_14d.reduce((s, r) => s + r.n, 0);
  const failuresActive = block.failures_total - block.failures_given_up;
  return (
    <section className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4 flex flex-col gap-3">
      <header>
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
          {categoryLabel(block)}
        </p>
        <p className="mt-2 font-mono tabular-nums text-[2rem] leading-none tracking-tight text-[var(--color-ink)]">
          {fmtCount(block.active_now)}
        </p>
        <p className="mt-1 text-[0.65rem] text-[var(--color-ink-4)] tracking-wide">active listings</p>
      </header>

      <div className="grid grid-cols-3 gap-3 pt-2 border-t border-[var(--color-rule-soft)]">
        <MiniStat
          label="new 14&thinsp;d"
          value={fmtCount(newTotal)}
          spark={block.new_per_day_14d}
          colour="copper"
        />
        <MiniStat
          label="flipped 7&thinsp;d"
          value={fmtCount(block.flipped_inactive_7d)}
          spark={block.flipped_per_day_7d}
          colour="brick"
        />
        <FailuresMini
          active={failuresActive}
          given_up={block.failures_given_up}
        />
      </div>
    </section>
  );
}

function MiniStat({
  label,
  value,
  spark,
  colour,
}: {
  label: ReactNode;
  value: ReactNode;
  spark: HealthDayCount[];
  colour: 'copper' | 'brick';
}) {
  return (
    <div className="min-w-0">
      <p className="text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
        {label}
      </p>
      <p className="mt-0.5 font-mono tabular-nums text-base text-[var(--color-ink)] leading-tight">
        {value}
      </p>
      <div className="mt-1">
        <Sparkline rows={spark} width={90} height={20} colour={colour} />
      </div>
    </div>
  );
}

function FailuresMini({
  active,
  given_up,
}: {
  active: number;
  given_up: number;
}) {
  return (
    <div className="min-w-0">
      <p className="text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
        failed
      </p>
      <p
        className="mt-0.5 font-mono tabular-nums text-base leading-tight"
        style={{ color: active > 0 ? 'var(--color-ochre)' : 'var(--color-ink)' }}
      >
        {fmtCount(active)}
      </p>
      <p className="mt-1 text-[0.6rem] text-[var(--color-ink-4)] tabular-nums leading-none">
        {given_up > 0 ? (
          <span style={{ color: 'var(--color-brick)' }}>
            {fmtCount(given_up)}&thinsp;given&nbsp;up
          </span>
        ) : (
          <span>0 given up</span>
        )}
      </p>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Card scaffolding                                                            */
/* -------------------------------------------------------------------------- */

function Card({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <section className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4">
      <h3 className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
        {label}
      </h3>
      <div className="mt-3">{children}</div>
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/* Snapshot density                                                            */
/* -------------------------------------------------------------------------- */

function SnapshotBars({
  rows,
  totalListings,
}: {
  rows: HealthSnapBucket[];
  totalListings: number;
}) {
  if (rows.length === 0) {
    return <p className="text-sm text-[var(--color-ink-4)]">No snapshots yet.</p>;
  }
  const max = Math.max(...rows.map((r) => r.n), 1);
  return (
    <ul className="space-y-2">
      {rows.map((r) => {
        const share = totalListings > 0 ? (r.n / totalListings) * 100 : 0;
        return (
          <li
            key={r.bucket}
            className="grid grid-cols-[2.2rem_1fr_5rem] gap-3 items-center"
          >
            <span className="font-mono tabular-nums text-sm text-[var(--color-ink-2)]">
              {r.bucket}
            </span>
            <div className="h-1.5 bg-[var(--color-rule-soft)] rounded-full overflow-hidden">
              <div
                className="h-full bg-[var(--color-copper)] rounded-full"
                style={{ width: `${(r.n / max) * 100}%` }}
              />
            </div>
            <div className="flex items-baseline justify-end gap-2">
              <span className="font-mono tabular-nums text-sm text-[var(--color-ink)]">
                {fmtCount(r.n)}
              </span>
              <span className="font-mono tabular-nums text-[0.65rem] text-[var(--color-ink-4)] w-9 text-right">
                {share.toFixed(0)}%
              </span>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

/* -------------------------------------------------------------------------- */
/* Freshness 24h horizontal bars                                              */
/* -------------------------------------------------------------------------- */

function FreshnessRows({ rows }: { rows: HealthFreshnessRow[] }) {
  if (rows.length === 0) {
    return (
      <p className="text-sm text-[var(--color-ink-4)] py-2">
        No verify-freshness calls in the last 24 hours.
      </p>
    );
  }
  const total = rows.reduce((s, r) => s + r.n, 0);
  const max = Math.max(...rows.map((r) => r.n), 1);
  return (
    <ul className="space-y-2">
      {rows.map((r) => {
        const share = total > 0 ? (r.n / total) * 100 : 0;
        return (
          <li
            key={r.outcome}
            className="grid grid-cols-[6rem_1fr_5rem] gap-3 items-center"
          >
            <span className="text-xs uppercase tracking-wide text-[var(--color-ink-2)] truncate">
              {r.outcome}
            </span>
            <div className="h-1.5 bg-[var(--color-rule-soft)] rounded-full overflow-hidden">
              <div
                className="h-full rounded-full"
                style={{
                  width: `${(r.n / max) * 100}%`,
                  backgroundColor: outcomeColour(r.outcome),
                }}
              />
            </div>
            <div className="flex items-baseline justify-end gap-2">
              <span className="font-mono tabular-nums text-sm text-[var(--color-ink)]">
                {fmtCount(r.n)}
              </span>
              <span className="font-mono tabular-nums text-[0.65rem] text-[var(--color-ink-4)] w-9 text-right">
                {share.toFixed(0)}%
              </span>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

function outcomeColour(outcome: string): string {
  switch (outcome) {
    case 'cached':       return 'var(--color-ink-3)';
    case 'unchanged':    return 'var(--color-sage)';
    case 'updated':      return 'var(--color-copper)';
    case 'gone':         return 'var(--color-ochre)';
    case 'fetch_error':  return 'var(--color-brick)';
    default:             return 'var(--color-ink-2)';
  }
}

/* -------------------------------------------------------------------------- */
/* Fetch failures                                                              */
/* -------------------------------------------------------------------------- */

function FailuresPanel({
  given_up,
  total,
  top10,
}: {
  given_up: number;
  total: number;
  top10: HealthFailureRow[];
}) {
  return (
    <div>
      <div className="flex items-baseline gap-6">
        <div>
          <p className="text-[0.62rem] tracking-[0.16em] uppercase text-[var(--color-ink-4)]">
            given up
          </p>
          <p
            className="mt-0.5 font-mono tabular-nums text-xl"
            style={{ color: given_up > 0 ? 'var(--color-brick)' : 'var(--color-ink)' }}
          >
            {fmtCount(given_up)}
          </p>
        </div>
        <div>
          <p className="text-[0.62rem] tracking-[0.16em] uppercase text-[var(--color-ink-4)]">
            active
          </p>
          <p className="mt-0.5 font-mono tabular-nums text-xl text-[var(--color-ink-2)]">
            {fmtCount(total - given_up)}
          </p>
        </div>
      </div>

      {top10.length > 0 ? (
        <div className="mt-4">
          <div className="overflow-x-auto -mx-1">
            <table className="w-full text-xs">
              <thead className="text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
                <tr>
                  <th className="text-left py-1.5 px-1.5 font-medium">ID</th>
                  <th className="text-right py-1.5 px-1.5 font-medium">Tries</th>
                  <th className="text-left  py-1.5 px-1.5 font-medium">Last fail</th>
                  <th className="text-left  py-1.5 px-1.5 font-medium">State</th>
                </tr>
              </thead>
              <tbody>
                {top10.map((r) => (
                  <tr key={r.sreality_id} className="border-t border-[var(--color-rule-soft)]">
                    <td className="py-1.5 px-1.5">
                      <Link
                        to={`/listing/${r.sreality_id}`}
                        className="font-mono tabular-nums text-[var(--color-copper)] hover:underline underline-offset-2"
                      >
                        {r.sreality_id}
                      </Link>
                    </td>
                    <td className="py-1.5 px-1.5 text-right font-mono tabular-nums text-[var(--color-ink)]">
                      {r.attempts}
                    </td>
                    <td
                      className="py-1.5 px-1.5 text-[var(--color-ink-2)] tabular-nums"
                      title={r.last_failure_at ? fmtAbsolute(r.last_failure_at) : undefined}
                    >
                      {r.last_failure_at ? fmtRelative(r.last_failure_at) : '—'}
                    </td>
                    <td className="py-1.5 px-1.5">
                      {r.given_up ? (
                        <span className="inline-flex items-center px-1.5 py-0.5 rounded-[var(--radius-xs)] text-[0.6rem] uppercase tracking-wide font-medium bg-[var(--color-brick-soft)] text-[var(--color-brick)]">
                          given&nbsp;up
                        </span>
                      ) : (
                        <span className="inline-flex items-center px-1.5 py-0.5 rounded-[var(--radius-xs)] text-[0.6rem] uppercase tracking-wide font-medium bg-[var(--color-ochre-soft)] text-[var(--color-ochre)]">
                          retrying
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : (
        <p className="mt-4 text-sm text-[var(--color-ink-4)]">No fetch failures recorded.</p>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Sparkline                                                                   */
/* -------------------------------------------------------------------------- */

function Sparkline({
  rows,
  width = 100,
  height = 30,
  colour = 'copper',
}: {
  rows: HealthDayCount[];
  width?: number;
  height?: number;
  colour?: 'copper' | 'brick';
}) {
  if (rows.length === 0) {
    return <span className="text-[0.65rem] text-[var(--color-ink-4)]">no data</span>;
  }
  const max = Math.max(...rows.map((r) => r.n), 1);
  const stepX = rows.length > 1 ? width / (rows.length - 1) : width;
  const points = rows
    .map((r, i) => {
      const x = i * stepX;
      const y = height - (r.n / max) * (height - 2) - 1;
      return `${x},${y}`;
    })
    .join(' ');
  const allZero = rows.every((r) => r.n === 0);
  const stroke = colour === 'brick' ? 'var(--color-brick)' : 'var(--color-copper)';
  return (
    <svg width={width} height={height} className="flex-shrink-0 block" aria-hidden>
      {!allZero && (
        <polyline
          fill="none"
          stroke={stroke}
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
          points={points}
        />
      )}
      <line
        x1="0"
        y1={height - 0.5}
        x2={width}
        y2={height - 0.5}
        stroke="var(--color-rule)"
        strokeWidth="1"
      />
    </svg>
  );
}

/* -------------------------------------------------------------------------- */
/* Loading skeleton                                                            */
/* -------------------------------------------------------------------------- */

function Skeleton() {
  return (
    <div className="mt-5 space-y-5 animate-pulse">
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <SkelCard h="11rem" />
        <SkelCard h="11rem" />
        <SkelCard h="11rem" />
        <SkelCard h="11rem" />
        <SkelCard h="11rem" />
        <SkelCard h="11rem" />
        <SkelCard h="11rem" />
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <SkelCard h="10rem" />
        <SkelCard h="10rem" />
      </div>
      <SkelCard h="16rem" />
    </div>
  );
}

function SkelCard({ h }: { h: string }) {
  return (
    <div
      className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)]"
      style={{ height: h }}
    />
  );
}

/* -------------------------------------------------------------------------- */
/* Recent scrapes (migration 086 — scrape_runs + recent_scrape_runs RPC)      */
/* -------------------------------------------------------------------------- */

const RUN_TIME_FMT = new Intl.DateTimeFormat('cs-CZ', {
  timeZone: 'Europe/Prague',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
});

const CHART_TIME_FMT = new Intl.DateTimeFormat('cs-CZ', {
  timeZone: 'Europe/Prague',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
});

function categoryPairLabel(
  cm: string | null,
  ct: string | null,
): string {
  const main = cm == null ? '—' : (CATEGORY_LABELS[cm] ?? cm);
  const type = ct == null ? '—' : (TYPE_LABELS[ct] ?? ct);
  return `${main} · ${type}`;
}

function RecentScrapesPanel({
  rows,
  isLoading,
  error,
}: {
  rows: ScrapeRun[] | undefined;
  isLoading: boolean;
  error: Error | null;
}) {
  if (error) {
    return (
      <p className="text-sm text-[var(--color-brick)]">
        recent_scrape_runs failed: {error.message}
      </p>
    );
  }
  if (isLoading && !rows) {
    return <p className="text-sm text-[var(--color-ink-4)]">Loading…</p>;
  }
  if (!rows || rows.length === 0) {
    return (
      <p className="text-sm text-[var(--color-ink-4)]">
        No scrape runs recorded yet. The next nightly or 15-minute scrape will
        land here.
      </p>
    );
  }

  /* Recharts wants ascending x-axis; the RPC returns most-recent-first. */
  const chartData = [...rows]
    .filter((r) => r.ended_at != null)
    .reverse()
    .map((r) => ({
      t: new Date(r.started_at).getTime(),
      scraped_new: r.listings_scraped_new,
      inactive: r.listings_inactive,
      images_stored: r.images_stored,
    }));

  return (
    <div>
      <div className="h-56 -mx-1 mb-4">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={chartData} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
            <CartesianGrid strokeDasharray="2 2" stroke="var(--color-rule-soft)" />
            <XAxis
              dataKey="t"
              type="number"
              scale="time"
              domain={['dataMin', 'dataMax']}
              tickFormatter={(t: number) => CHART_TIME_FMT.format(new Date(t))}
              tick={{ fill: 'var(--color-ink-3)', fontSize: 11 }}
              stroke="var(--color-rule)"
            />
            <YAxis
              tick={{ fill: 'var(--color-ink-3)', fontSize: 11 }}
              stroke="var(--color-rule)"
              allowDecimals={false}
            />
            <Tooltip
              contentStyle={{
                background: 'var(--color-paper)',
                border: '1px solid var(--color-rule)',
                borderRadius: 6,
                fontSize: 12,
              }}
              labelFormatter={(t) =>
                CHART_TIME_FMT.format(new Date(Number(t)))
              }
              formatter={(v: number, name: string) => [fmtCount(v), name]}
            />
            <Legend
              wrapperStyle={{ fontSize: 11, color: 'var(--color-ink-2)' }}
              iconType="line"
            />
            <Line
              type="monotone"
              dataKey="scraped_new"
              name="new"
              stroke="var(--color-copper)"
              strokeWidth={1.5}
              dot={false}
            />
            <Line
              type="monotone"
              dataKey="inactive"
              name="inactive"
              stroke="var(--color-brick)"
              strokeWidth={1.5}
              dot={false}
            />
            <Line
              type="monotone"
              dataKey="images_stored"
              name="images stored"
              stroke="var(--color-sage)"
              strokeWidth={1.5}
              dot={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>

      <div className="overflow-x-auto -mx-1">
        <table className="w-full text-xs">
          <thead className="text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
            <tr>
              <th className="text-left  py-1.5 px-1.5 font-medium w-4"></th>
              <th className="text-left  py-1.5 px-1.5 font-medium">Time</th>
              <th className="text-left  py-1.5 px-1.5 font-medium">Type</th>
              <th className="text-right py-1.5 px-1.5 font-medium">Found new</th>
              <th className="text-right py-1.5 px-1.5 font-medium">Scraped new</th>
              <th className="text-right py-1.5 px-1.5 font-medium">Inactive</th>
              <th className="text-right py-1.5 px-1.5 font-medium">Imgs disc.</th>
              <th className="text-right py-1.5 px-1.5 font-medium">Imgs stored</th>
              <th className="text-right py-1.5 px-1.5 font-medium">Errors</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <ScrapeRunRow key={r.id} run={r} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ScrapeRunRow({ run }: { run: ScrapeRun }) {
  const [open, setOpen] = useState(false);
  const hasBreakdown = run.by_category.length > 0;
  return (
    <>
      <tr
        className="border-t border-[var(--color-rule-soft)] hover:bg-[var(--color-paper-3)]/40"
      >
        <td className="py-1.5 px-1.5 align-top">
          <button
            type="button"
            onClick={() => hasBreakdown && setOpen((v) => !v)}
            className="font-mono text-[var(--color-ink-3)] hover:text-[var(--color-copper)] disabled:text-[var(--color-ink-4)] w-3 leading-none"
            disabled={!hasBreakdown}
            aria-label={open ? 'collapse' : 'expand'}
          >
            {hasBreakdown ? (open ? '▾' : '▸') : '·'}
          </button>
        </td>
        <td
          className="py-1.5 px-1.5 font-mono tabular-nums text-[var(--color-ink)] align-top"
          title={fmtAbsolute(run.started_at)}
        >
          {RUN_TIME_FMT.format(new Date(run.started_at))}
          {run.ended_at == null && (
            <span className="ml-1 text-[var(--color-ochre)]" title="never finalised">
              ⚠
            </span>
          )}
        </td>
        <td className="py-1.5 px-1.5 align-top">
          <span
            className={
              'inline-flex items-center px-1.5 py-0.5 rounded-[var(--radius-xs)] text-[0.6rem] uppercase tracking-wide font-medium ' +
              (run.run_type === 'full'
                ? 'bg-[var(--color-copper-soft)] text-[var(--color-copper)]'
                : 'bg-[var(--color-rule-soft)] text-[var(--color-ink-2)]')
            }
          >
            {run.run_type}
          </span>
        </td>
        <td className="py-1.5 px-1.5 text-right font-mono tabular-nums align-top">
          {fmtCount(run.listings_found_new)}
        </td>
        <td className="py-1.5 px-1.5 text-right font-mono tabular-nums align-top">
          {fmtCount(run.listings_scraped_new)}
        </td>
        <td className="py-1.5 px-1.5 text-right font-mono tabular-nums align-top">
          {fmtCount(run.listings_inactive)}
        </td>
        <td className="py-1.5 px-1.5 text-right font-mono tabular-nums align-top">
          {fmtCount(run.images_discovered)}
        </td>
        <td className="py-1.5 px-1.5 text-right font-mono tabular-nums align-top">
          {fmtCount(run.images_stored)}
        </td>
        <td
          className="py-1.5 px-1.5 text-right font-mono tabular-nums align-top"
          style={{ color: run.errors > 0 ? 'var(--color-brick)' : undefined }}
        >
          {fmtCount(run.errors)}
        </td>
      </tr>
      {open &&
        run.by_category.map((c) => (
          <ScrapeRunCategoryRow
            key={`${run.id}-${c.category_main}-${c.category_type}`}
            cat={c}
          />
        ))}
    </>
  );
}

function ScrapeRunCategoryRow({ cat }: { cat: ScrapeRunCategory }) {
  return (
    <tr className="border-t border-[var(--color-rule-soft)] bg-[var(--color-paper-3)]/30">
      <td className="py-1 px-1.5"></td>
      <td
        className="py-1 px-1.5 text-[var(--color-ink-2)] text-[0.7rem]"
        colSpan={2}
      >
        ↳ {categoryPairLabel(cat.category_main, cat.category_type)}
      </td>
      <td className="py-1 px-1.5 text-right font-mono tabular-nums text-[0.7rem] text-[var(--color-ink-2)]">
        {fmtCount(cat.listings_found_new)}
      </td>
      <td className="py-1 px-1.5 text-right font-mono tabular-nums text-[0.7rem] text-[var(--color-ink-2)]">
        {fmtCount(cat.listings_scraped_new)}
      </td>
      <td className="py-1 px-1.5 text-right font-mono tabular-nums text-[0.7rem] text-[var(--color-ink-2)]">
        {fmtCount(cat.listings_inactive)}
      </td>
      <td className="py-1 px-1.5 text-right font-mono tabular-nums text-[0.7rem] text-[var(--color-ink-2)]">
        {fmtCount(cat.images_discovered)}
      </td>
      <td className="py-1 px-1.5 text-right font-mono tabular-nums text-[0.7rem] text-[var(--color-ink-2)]">
        {fmtCount(cat.images_stored)}
      </td>
      <td className="py-1 px-1.5"></td>
    </tr>
  );
}

/* -------------------------------------------------------------------------- */
/* Image mirror overview (image_storage_overview RPC)                          */
/* -------------------------------------------------------------------------- */

function ImageMirrorPanel({
  overview,
  isLoading,
  error,
}: {
  overview: ImageStorageOverview | undefined;
  isLoading: boolean;
  error: Error | null;
}) {
  if (error) {
    return (
      <p className="text-sm text-[var(--color-brick)]">
        image_storage_overview failed: {error.message}
      </p>
    );
  }
  if (isLoading && !overview) {
    return <p className="text-sm text-[var(--color-ink-4)]">Loading…</p>;
  }
  if (!overview) return null;
  const pct =
    overview.total_images > 0
      ? (overview.stored_images / overview.total_images) * 100
      : 0;
  const rows = [...overview.by_category].sort(
    (a, b) => b.total - a.total,
  );
  return (
    <div>
      <div className="flex items-baseline justify-between gap-4">
        <p className="text-[0.62rem] tracking-[0.16em] uppercase text-[var(--color-ink-4)]">
          stored / total
        </p>
        <p className="font-mono tabular-nums text-sm text-[var(--color-ink)]">
          {fmtCount(overview.stored_images)}
          <span className="text-[var(--color-ink-4)]">
            {' / '}
            {fmtCount(overview.total_images)}
          </span>
          <span className="ml-2 text-[var(--color-ink-3)] text-[0.65rem]">
            {pct.toFixed(1)}%
          </span>
        </p>
      </div>
      <div className="mt-1.5 h-1.5 bg-[var(--color-rule-soft)] rounded-full overflow-hidden">
        <div
          className="h-full bg-[var(--color-copper)] rounded-full"
          style={{ width: `${pct}%` }}
        />
      </div>

      <table className="w-full text-xs mt-4">
        <thead className="text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
          <tr>
            <th className="text-left  py-1.5 px-1.5 font-medium">Category</th>
            <th className="text-right py-1.5 px-1.5 font-medium">Total</th>
            <th className="text-right py-1.5 px-1.5 font-medium">Stored</th>
            <th className="text-right py-1.5 px-1.5 font-medium w-12">%</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <ImageStorageRow key={`${r.category_main}-${r.category_type}`} row={r} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ImageStorageRow({ row }: { row: ImageStorageCategory }) {
  const pct = row.total > 0 ? (row.stored / row.total) * 100 : 0;
  return (
    <tr className="border-t border-[var(--color-rule-soft)]">
      <td className="py-1.5 px-1.5 text-[var(--color-ink)]">
        {categoryPairLabel(row.category_main, row.category_type)}
      </td>
      <td className="py-1.5 px-1.5 text-right font-mono tabular-nums text-[var(--color-ink)]">
        {fmtCount(row.total)}
      </td>
      <td className="py-1.5 px-1.5 text-right font-mono tabular-nums text-[var(--color-ink)]">
        {fmtCount(row.stored)}
      </td>
      <td className="py-1.5 px-1.5 text-right font-mono tabular-nums text-[var(--color-ink-3)]">
        {row.total > 0 ? `${pct.toFixed(0)}%` : '—'}
      </td>
    </tr>
  );
}

/* -------------------------------------------------------------------------- */
/* Scrape schedule (static config — mirrors the GitHub Actions cron lines)     */
/* -------------------------------------------------------------------------- */

function SchedulePanel() {
  return (
    <dl className="space-y-2 text-sm">
      <ScheduleRow
        label="Full scrape"
        cron="0 22 * * *"
        human="Daily at 22:00 UTC"
        note="Walks all six category pairs end-to-end; the only path that marks listings inactive. Runs the image-download phase and condition scoring after the scrape."
      />
      <ScheduleRow
        label="Delta scrape"
        cron="*/15 * * * *"
        human="Every 15 minutes"
        note="--limit 200 per category — picks up new listings within minutes. Skips image downloads and condition scoring; never marks listings inactive."
      />
    </dl>
  );
}

function ScheduleRow({
  label,
  cron,
  human,
  note,
}: {
  label: string;
  cron: string;
  human: string;
  note: string;
}) {
  return (
    <div className="border-t first:border-t-0 first:pt-0 pt-2 border-[var(--color-rule-soft)]">
      <div className="flex items-baseline justify-between gap-3">
        <dt className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
          {label}
        </dt>
        <dd className="font-mono text-[0.65rem] text-[var(--color-ink-4)] tabular-nums">
          {cron}
        </dd>
      </div>
      <p className="mt-0.5 font-mono tabular-nums text-[var(--color-ink)]">
        {human}
      </p>
      <p className="mt-1 text-xs text-[var(--color-ink-3)] leading-snug">
        {note}
      </p>
    </div>
  );
}
