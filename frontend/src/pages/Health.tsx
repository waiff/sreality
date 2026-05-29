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
  fetchPortalHealth,
  fetchRecentScrapeRuns,
  fetchScraperHealthChecks,
} from '@/lib/queries';
import type {
  HealthSummary,
  HealthDayCount,
  HealthSnapBucket,
  HealthFreshnessRow,
  HealthFailureRow,
  HealthCategoryBlock,
  HealthCheckStatus,
  ImageStorageCategory,
  ImageStorageOverview,
  PortalHealth,
  PortalKind,
  PortalStage,
  ScrapeRun,
  ScrapeRunCategory,
  ScraperHealthCheck,
  ScraperHealthChecks,
} from '@/lib/types';
import { fmtCount, fmtRelative, fmtAbsolute } from '@/lib/format';
import { WORKFLOW_DOCS } from '@/lib/workflowDocs.generated';

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
            Data sources, scraper status by category, snapshot density, fetch failures.{' '}
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
  const healthChecksQuery = useQuery<ScraperHealthChecks, Error>({
    queryKey: ['scraper-health-checks'],
    queryFn: fetchScraperHealthChecks,
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
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
      <PortalsSection />

      <Card label="Scraper health checks · sreality pipeline">
        <HealthChecksPanel
          checks={healthChecksQuery.data}
          isLoading={healthChecksQuery.isLoading}
          error={healthChecksQuery.error}
        />
      </Card>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <LastScrapeTile lastScrapeAt={data.last_scrape_at} />
      </div>

      <Card label="Listings by category · reconciliation">
        <CategoryTable liveByCategory={data.by_category} rows={scrapeRunsQuery.data} />
      </Card>

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
/* Data-source catalogue (migration 100 — portal_health_summary RPC)          */
/*                                                                            */
/* A roll-call of every portal the platform pulls from. Each portal is a      */
/* register entry: name in the display serif, a quiet kind/stage tag, and a   */
/* headline number whose meaning shifts by kind — active listings for         */
/* scrapers, URLs parsed for on-demand parsers. New portals appear the moment */
/* a row is added to the `portals` table; never-run ones show at zero rather  */
/* than vanishing, so a dormant pilot stays visible.                          */
/* -------------------------------------------------------------------------- */

const PORTAL_KIND_LABEL: Record<PortalKind, string> = {
  scraper: 'scraper',
  parser: 'on-demand parser',
};

const PORTAL_STAGE_LABEL: Record<PortalStage, string> = {
  live: 'live',
  pilot: 'pilot',
  on_demand: 'on demand',
  planned: 'planned',
};

/* Short labels for the per-run "Site" tag. Falls back to the raw source. */
const PORTAL_SHORT_LABEL: Record<string, string> = {
  sreality: 'Sreality',
  bazos: 'Bazoš',
  idnes: 'iDNES',
};

function portalShort(source: string): string {
  return PORTAL_SHORT_LABEL[source] ?? source;
}

/* Status dot. For scrapers, recency of the last run is meaningful (the cron
 * should keep it fresh); for on-demand parsers it is not — they only run when
 * the operator pastes a URL — so a parser that has ever run reads neutral,
 * never "stale". A never-run portal is muted, not alarming. */
function portalDotColour(p: PortalHealth): string {
  if (p.kind === 'scraper') {
    if (!p.last_scrape_at) return 'var(--color-ink-4)';
    const ageMin = (Date.now() - new Date(p.last_scrape_at).getTime()) / 60_000;
    if (ageMin < 90) return 'var(--color-sage)';
    if (ageMin < 24 * 60) return 'var(--color-ochre)';
    return 'var(--color-brick)';
  }
  return p.last_parsed_at ? 'var(--color-copper)' : 'var(--color-ink-4)';
}

function PortalsSection() {
  const { data, isLoading, error } = useQuery<PortalHealth[], Error>({
    queryKey: ['portal-health'],
    queryFn: fetchPortalHealth,
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const scrapers = (data ?? []).filter((p) => p.kind === 'scraper').length;
  const parsers = (data ?? []).filter((p) => p.kind === 'parser').length;

  return (
    <section>
      <div className="flex items-baseline justify-between gap-4">
        <h2 className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
          Data sources
        </h2>
        {data && data.length > 0 && (
          <span className="text-[0.65rem] text-[var(--color-ink-4)] tabular-nums">
            {scrapers} scraper{scrapers === 1 ? '' : 's'} · {parsers} on-demand parser
            {parsers === 1 ? '' : 's'}
          </span>
        )}
      </div>

      <div className="mt-3">
        {error ? (
          <p className="text-sm text-[var(--color-brick)]">
            portal_health_summary failed: {error.message}
          </p>
        ) : isLoading && !data ? (
          <p className="text-sm text-[var(--color-ink-3)]">Loading sources…</p>
        ) : !data || data.length === 0 ? (
          <p className="text-sm text-[var(--color-ink-4)]">No portals registered.</p>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {data.map((p) => (
              <PortalCard key={p.source} portal={p} />
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

function PortalCard({ portal: p }: { portal: PortalHealth }) {
  const isScraper = p.kind === 'scraper';
  const hasHeadline = isScraper ? p.listings_active > 0 : p.parses_total > 0;
  const headline = isScraper
    ? fmtCount(p.listings_active)
    : fmtCount(p.parses_total);
  const headlineLabel = isScraper
    ? hasHeadline
      ? 'active listings'
      : 'no listings yet'
    : hasHeadline
      ? 'URLs parsed'
      : 'never parsed';

  return (
    <section className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4 flex flex-col gap-3">
      <header className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <span
            className="inline-block h-2 w-2 rounded-full shrink-0"
            style={{ backgroundColor: portalDotColour(p) }}
          />
          {p.home_url ? (
            <a
              href={p.home_url}
              target="_blank"
              rel="noreferrer"
              className="font-display text-lg leading-tight text-[var(--color-ink)] hover:text-[var(--color-copper)] truncate"
            >
              {p.label}
            </a>
          ) : (
            <span className="font-display text-lg leading-tight text-[var(--color-ink)] truncate">
              {p.label}
            </span>
          )}
        </div>
        <span className="shrink-0 text-right text-[0.58rem] tracking-[0.1em] uppercase leading-tight">
          <span className="text-[var(--color-ink-3)]">{PORTAL_KIND_LABEL[p.kind]}</span>
          <br />
          <span className="text-[var(--color-ink-4)]">{PORTAL_STAGE_LABEL[p.stage]}</span>
        </span>
      </header>

      <div>
        <p
          className="font-mono tabular-nums text-[1.7rem] leading-none tracking-tight"
          style={{ color: hasHeadline ? 'var(--color-ink)' : 'var(--color-ink-4)' }}
        >
          {hasHeadline ? headline : '—'}
        </p>
        <p className="mt-1 text-[0.65rem] text-[var(--color-ink-4)] tracking-wide">
          {headlineLabel}
        </p>
      </div>

      <div className="grid grid-cols-3 gap-3 pt-2 border-t border-[var(--color-rule-soft)]">
        {isScraper ? (
          <>
            <PortalStat label="new 7&thinsp;d" value={fmtCount(p.scraped_new_7d)} />
            <PortalStat label="runs 7&thinsp;d" value={fmtCount(p.runs_7d)} />
            <PortalStat
              label="last scrape"
              value={p.last_scrape_at ? fmtRelative(p.last_scrape_at) : '—'}
              title={p.last_scrape_at ? fmtAbsolute(p.last_scrape_at) : undefined}
            />
          </>
        ) : (
          <>
            <PortalStat label="parsed 30&thinsp;d" value={fmtCount(p.parses_30d)} />
            <PortalStat label="total" value={fmtCount(p.parses_total)} />
            <PortalStat
              label="last parse"
              value={p.last_parsed_at ? fmtRelative(p.last_parsed_at) : '—'}
              title={p.last_parsed_at ? fmtAbsolute(p.last_parsed_at) : undefined}
            />
          </>
        )}
      </div>
    </section>
  );
}

function PortalStat({
  label,
  value,
  title,
}: {
  label: ReactNode;
  value: ReactNode;
  title?: string;
}) {
  return (
    <div className="min-w-0">
      <p className="text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
        {label}
      </p>
      <p
        className="mt-0.5 font-mono tabular-nums text-sm text-[var(--color-ink)] leading-tight truncate"
        title={title}
      >
        {value}
      </p>
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
/* Per-category table + reconciliation (one row per category)                 */
/*                                                                            */
/* Folds the old 6-tile grid and the separate Count-Reconciliation table into */
/* one place — each category appears once with both its activity (active /     */
/* new / flipped / failed) and its reconciliation against sreality. After the  */
/* index/detail split the honest reconciliation is two distinct things:        */
/*   · Index  — did the walk SEE every listing (collected vs sreality total).  */
/*   · Queue  — seen but not yet FETCHED by the detail-drain (the real lag      */
/*     behind any apparent "drift"; a new listing is active only once drained).*/
/* -------------------------------------------------------------------------- */

function CategoryTable({
  liveByCategory,
  rows,
}: {
  liveByCategory: HealthCategoryBlock[];
  rows: ScrapeRun[] | undefined;
}) {
  // The latest index run carrying per-category result_size (rows are
  // most-recent-first). Supplies sreality's total + what we collected.
  const run = (rows ?? []).find((r) =>
    r.by_category?.some((c) => c.sreality_result_size != null),
  );
  const reconByCat = new Map<string, ScrapeRunCategory>();
  run?.by_category.forEach((c) =>
    reconByCat.set(`${c.category_main}-${c.category_type}`, c),
  );

  const cats = [...liveByCategory].sort((a, b) => b.active_now - a.active_now);

  return (
    <div>
      <p className="mb-2 text-xs text-[var(--color-ink-3)] leading-snug">
        <span className="text-[var(--color-ink-2)]">sreality</span> = the portal&rsquo;s
        reported total{run ? <> (probed {fmtRelative(run.started_at)})</> : null};{' '}
        <span className="text-[var(--color-ink-2)]">Index</span> = share of those listings
        the walk collected; <span className="text-[var(--color-ink-2)]">Queue</span> = seen
        but not yet fetched by the detail-drain (the real lag behind any apparent drift).
      </p>
      <div className="overflow-x-auto -mx-1">
        <table className="w-full text-xs">
          <thead className="text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
            <tr>
              <th className="text-left  py-1.5 px-1.5 font-medium">Category</th>
              <th className="text-right py-1.5 px-1.5 font-medium">Active</th>
              <th className="text-right py-1.5 px-1.5 font-medium">sreality</th>
              <th className="text-right py-1.5 px-1.5 font-medium">Collected</th>
              <th className="text-right py-1.5 px-1.5 font-medium">Index</th>
              <th className="text-right py-1.5 px-1.5 font-medium">Queue</th>
              <th className="text-left  py-1.5 px-1.5 font-medium">new 14&thinsp;d</th>
              <th className="text-left  py-1.5 px-1.5 font-medium">flipped 7&thinsp;d</th>
              <th className="text-right py-1.5 px-1.5 font-medium">failed</th>
            </tr>
          </thead>
          <tbody>
            {cats.map((b) => (
              <CategoryTableRow
                key={`${b.category_main}-${b.category_type}`}
                block={b}
                recon={reconByCat.get(`${b.category_main}-${b.category_type}`)}
              />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function CategoryTableRow({
  block,
  recon,
}: {
  block: HealthCategoryBlock;
  recon: ScrapeRunCategory | undefined;
}) {
  const newTotal = block.new_per_day_14d.reduce((s, r) => s + r.n, 0);
  const failuresActive = block.failures_total - block.failures_given_up;
  const srealityTotal = recon?.sreality_result_size ?? null;
  const collected = recon?.collected ?? null;
  const indexPct =
    srealityTotal && srealityTotal > 0 && collected != null
      ? (collected / srealityTotal) * 100
      : null;
  const indexColour =
    indexPct == null
      ? 'var(--color-ink-4)'
      : indexPct >= 99
        ? 'var(--color-sage)'
        : indexPct >= 95
          ? 'var(--color-ochre)'
          : 'var(--color-brick)';
  // Detail-drain backlog proxy: seen (collected, or sreality's total) minus
  // what is currently active. Per-category exact queue depth isn't available
  // (un-drained ids aren't listings rows yet), so this is the honest estimate.
  const seen = collected ?? srealityTotal;
  const queue = seen != null ? Math.max(0, seen - block.active_now) : null;
  return (
    <tr className="border-t border-[var(--color-rule-soft)] hover:bg-[var(--color-paper-3)]/40">
      <td className="py-1.5 px-1.5 text-[var(--color-ink)] whitespace-nowrap">
        {categoryLabel(block)}
      </td>
      <td className="py-1.5 px-1.5 text-right font-mono tabular-nums text-[var(--color-ink)]">
        {fmtCount(block.active_now)}
      </td>
      <td className="py-1.5 px-1.5 text-right font-mono tabular-nums text-[var(--color-ink-2)]">
        {srealityTotal != null ? fmtCount(srealityTotal) : '—'}
      </td>
      <td className="py-1.5 px-1.5 text-right font-mono tabular-nums text-[var(--color-ink-2)]">
        {collected != null ? fmtCount(collected) : '—'}
      </td>
      <td
        className="py-1.5 px-1.5 text-right font-mono tabular-nums"
        style={{ color: indexColour }}
      >
        {indexPct != null ? `${indexPct.toFixed(0)}%` : '—'}
      </td>
      <td
        className="py-1.5 px-1.5 text-right font-mono tabular-nums"
        style={{ color: queue && queue > 1000 ? 'var(--color-ochre)' : 'var(--color-ink-3)' }}
        title="seen in the index but not yet fetched by the detail-drain"
      >
        {queue != null ? fmtCount(queue) : '—'}
      </td>
      <td className="py-1.5 px-1.5">
        <div className="flex items-center gap-2">
          <span className="font-mono tabular-nums text-[var(--color-ink)] w-10 text-right">
            {fmtCount(newTotal)}
          </span>
          <Sparkline rows={block.new_per_day_14d} width={64} height={18} colour="copper" />
        </div>
      </td>
      <td className="py-1.5 px-1.5">
        <div className="flex items-center gap-2">
          <span className="font-mono tabular-nums text-[var(--color-ink)] w-8 text-right">
            {fmtCount(block.flipped_inactive_7d)}
          </span>
          <Sparkline rows={block.flipped_per_day_7d} width={64} height={18} colour="brick" />
        </div>
      </td>
      <td className="py-1.5 px-1.5 text-right font-mono tabular-nums leading-tight">
        <span style={{ color: failuresActive > 0 ? 'var(--color-ochre)' : 'var(--color-ink)' }}>
          {fmtCount(failuresActive)}
        </span>
        {block.failures_given_up > 0 && (
          <span className="block text-[0.6rem] text-[var(--color-brick)]">
            {fmtCount(block.failures_given_up)}&thinsp;given&nbsp;up
          </span>
        )}
      </td>
    </tr>
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
/* Scraper health checks                                                       */
/* -------------------------------------------------------------------------- */

const STATUS_STYLES: Record<HealthCheckStatus, { pill: string; dot: string; label: string }> = {
  pass: {
    pill: 'bg-[var(--color-sage-soft)] text-[var(--color-sage)]',
    dot: 'bg-[var(--color-sage)]',
    label: 'OK',
  },
  warn: {
    pill: 'bg-[var(--color-ochre-soft)] text-[var(--color-ochre)]',
    dot: 'bg-[var(--color-ochre)]',
    label: 'Watch',
  },
  fail: {
    pill: 'bg-[var(--color-brick-soft)] text-[var(--color-brick)]',
    dot: 'bg-[var(--color-brick)]',
    label: 'Problem',
  },
};

function HealthChecksPanel({
  checks,
  isLoading,
  error,
}: {
  checks: ScraperHealthChecks | undefined;
  isLoading: boolean;
  error: Error | null;
}) {
  if (error) {
    return (
      <p className="text-sm text-[var(--color-brick)]">
        scraper_health_checks failed: {error.message}
      </p>
    );
  }
  if (isLoading && !checks) {
    return <p className="text-sm text-[var(--color-ink-3)]">Loading checks…</p>;
  }
  if (!checks || checks.checks.length === 0) {
    return <p className="text-sm text-[var(--color-ink-3)]">No checks available.</p>;
  }

  const counts = checks.checks.reduce(
    (acc, c) => ({ ...acc, [c.status]: acc[c.status] + 1 }),
    { pass: 0, warn: 0, fail: 0 } as Record<HealthCheckStatus, number>,
  );
  const order: Record<HealthCheckStatus, number> = { fail: 0, warn: 1, pass: 2 };
  const sorted = [...checks.checks].sort((a, b) => order[a.status] - order[b.status]);

  return (
    <div>
      <div className="flex items-center gap-3 text-xs text-[var(--color-ink-2)]">
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-full bg-[var(--color-sage)]" />
          {counts.pass} OK
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-full bg-[var(--color-ochre)]" />
          {counts.warn} watch
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-full bg-[var(--color-brick)]" />
          {counts.fail} problem
        </span>
        <span className="ml-auto text-[var(--color-ink-3)]">
          checked {fmtRelative(checks.generated_at)}
        </span>
      </div>

      <div className="mt-3 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
        {sorted.map((c) => (
          <HealthCheckCard key={c.key} check={c} />
        ))}
      </div>
    </div>
  );
}

function HealthCheckCard({ check }: { check: ScraperHealthCheck }) {
  const s = STATUS_STYLES[check.status] ?? STATUS_STYLES.warn;
  return (
    <div className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-3 py-2.5">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium text-[var(--color-ink)]">{check.label}</span>
        <span
          className={
            'inline-flex items-center px-1.5 py-0.5 rounded-[var(--radius-xs)] text-[0.6rem] uppercase tracking-wide font-medium ' +
            s.pill
          }
        >
          {s.label}
        </span>
      </div>
      <div className="mt-1 flex items-baseline gap-2">
        <span className={'inline-block h-2 w-2 rounded-full shrink-0 ' + s.dot} />
        <span className="text-base tabular-nums text-[var(--color-ink)]">{check.value}</span>
      </div>
      <p className="mt-1.5 text-[0.7rem] leading-snug text-[var(--color-ink-3)]">
        {check.detail}
      </p>
    </div>
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

const RECENT_RUNS_VISIBLE = 15;

/* index = the cheap completeness walk; detail = the slow per-listing drain;
 * full/delta = the legacy monolithic scraper. */
const RUN_TYPE_PILL: Record<ScrapeRun['run_type'], string> = {
  index: 'bg-[var(--color-copper-soft)] text-[var(--color-copper)]',
  detail: 'bg-[var(--color-sage-soft)] text-[var(--color-sage)]',
  full: 'bg-[var(--color-copper-soft)] text-[var(--color-copper)]',
  delta: 'bg-[var(--color-rule-soft)] text-[var(--color-ink-2)]',
};

function RecentScrapesPanel({
  rows,
  isLoading,
  error,
}: {
  rows: ScrapeRun[] | undefined;
  isLoading: boolean;
  error: Error | null;
}) {
  const [showAll, setShowAll] = useState(false);
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
              <th className="text-left  py-1.5 px-1.5 font-medium">Site</th>
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
            {(showAll ? rows : rows.slice(0, RECENT_RUNS_VISIBLE)).map((r) => (
              <ScrapeRunRow key={r.id} run={r} />
            ))}
          </tbody>
        </table>
      </div>

      {rows.length > RECENT_RUNS_VISIBLE && (
        <button
          type="button"
          onClick={() => setShowAll((v) => !v)}
          className="mt-3 text-xs text-[var(--color-copper)] hover:underline"
        >
          {showAll
            ? `Show fewer (latest ${RECENT_RUNS_VISIBLE})`
            : `Show all ${fmtCount(rows.length)} runs`}
        </button>
      )}
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
        <td className="py-1.5 px-1.5 align-top text-[var(--color-ink-2)] whitespace-nowrap">
          {portalShort(run.source)}
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
              RUN_TYPE_PILL[run.run_type]
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
        colSpan={3}
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
  const activePct =
    overview.total_active_images > 0
      ? (overview.stored_active_images / overview.total_active_images) * 100
      : 0;
  // Focus the active subset — those CDN photos are still fetchable, so the gap
  // is closeable; inactive listings' photos are mostly expired.
  const rows = [...overview.by_category].sort(
    (a, b) => b.total_active - a.total_active,
  );
  return (
    <div>
      <ImageBar
        label="active listings · closeable gap"
        stored={overview.stored_active_images}
        total={overview.total_active_images}
        pct={activePct}
        emphasis
      />
      <div className="mt-3">
        <ImageBar
          label="all listings (incl. inactive — mostly expired)"
          stored={overview.stored_images}
          total={overview.total_images}
          pct={pct}
        />
      </div>

      <table className="w-full text-xs mt-4">
        <thead className="text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
          <tr>
            <th className="text-left  py-1.5 px-1.5 font-medium">Category</th>
            <th className="text-right py-1.5 px-1.5 font-medium">Active stored</th>
            <th className="text-right py-1.5 px-1.5 font-medium">Active total</th>
            <th className="text-right py-1.5 px-1.5 font-medium w-12">Active&thinsp;%</th>
            <th className="text-right py-1.5 px-1.5 font-medium text-[var(--color-ink-4)]">All stored</th>
            <th className="text-right py-1.5 px-1.5 font-medium text-[var(--color-ink-4)]">All total</th>
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

function ImageBar({
  label,
  stored,
  total,
  pct,
  emphasis = false,
}: {
  label: string;
  stored: number;
  total: number;
  pct: number;
  emphasis?: boolean;
}) {
  return (
    <div>
      <div className="flex items-baseline justify-between gap-4">
        <p className="text-[0.62rem] tracking-[0.16em] uppercase text-[var(--color-ink-4)]">
          {label}
        </p>
        <p className="font-mono tabular-nums text-sm text-[var(--color-ink)]">
          {fmtCount(stored)}
          <span className="text-[var(--color-ink-4)]">
            {' / '}
            {fmtCount(total)}
          </span>
          <span className="ml-2 text-[var(--color-ink-3)] text-[0.65rem]">
            {pct.toFixed(1)}%
          </span>
        </p>
      </div>
      <div className="mt-1.5 h-1.5 bg-[var(--color-rule-soft)] rounded-full overflow-hidden">
        <div
          className="h-full rounded-full"
          style={{
            width: `${pct}%`,
            background: emphasis ? 'var(--color-copper)' : 'var(--color-ink-3)',
          }}
        />
      </div>
    </div>
  );
}

function ImageStorageRow({ row }: { row: ImageStorageCategory }) {
  const activePct = row.total_active > 0 ? (row.stored_active / row.total_active) * 100 : 0;
  const activeColour =
    row.total_active === 0
      ? 'var(--color-ink-4)'
      : activePct >= 80
        ? 'var(--color-sage)'
        : activePct >= 40
          ? 'var(--color-ochre)'
          : 'var(--color-brick)';
  return (
    <tr className="border-t border-[var(--color-rule-soft)]">
      <td className="py-1.5 px-1.5 text-[var(--color-ink)]">
        {categoryPairLabel(row.category_main, row.category_type)}
      </td>
      <td className="py-1.5 px-1.5 text-right font-mono tabular-nums text-[var(--color-ink)]">
        {fmtCount(row.stored_active)}
      </td>
      <td className="py-1.5 px-1.5 text-right font-mono tabular-nums text-[var(--color-ink)]">
        {fmtCount(row.total_active)}
      </td>
      <td
        className="py-1.5 px-1.5 text-right font-mono tabular-nums"
        style={{ color: activeColour }}
      >
        {row.total_active > 0 ? `${activePct.toFixed(0)}%` : '—'}
      </td>
      <td className="py-1.5 px-1.5 text-right font-mono tabular-nums text-[var(--color-ink-4)]">
        {fmtCount(row.stored)}
      </td>
      <td className="py-1.5 px-1.5 text-right font-mono tabular-nums text-[var(--color-ink-4)]">
        {fmtCount(row.total)}
      </td>
    </tr>
  );
}

/* -------------------------------------------------------------------------- */
/* Scrape schedule (data-driven from workflowDocs.generated.ts — the codegen   */
/* of the .github/workflows/*.yml cron lines, so it can never go stale)        */
/* -------------------------------------------------------------------------- */

/* Which scheduled workflows are scrapes vs background maintenance jobs. */
const SCRAPE_WORKFLOW_FILES = new Set([
  'index_walk.yml',
  'detail_drain.yml',
  'scrape.yml',
  'scrape_bazos.yml',
]);

function SchedulePanel() {
  const scheduled = WORKFLOW_DOCS.filter((w) => w.schedules.length > 0).sort((a, b) =>
    a.name.localeCompare(b.name),
  );
  const scrapes = scheduled.filter((w) => SCRAPE_WORKFLOW_FILES.has(w.filename));
  const jobs = scheduled.filter((w) => !SCRAPE_WORKFLOW_FILES.has(w.filename));
  return (
    <div className="space-y-4">
      <ScheduleGroup title="Scrapes" items={scrapes} />
      <ScheduleGroup title="Maintenance jobs" items={jobs} />
    </div>
  );
}

function ScheduleGroup({
  title,
  items,
}: {
  title: string;
  items: typeof WORKFLOW_DOCS;
}) {
  if (items.length === 0) return null;
  return (
    <div>
      <p className="text-[0.6rem] tracking-[0.16em] uppercase text-[var(--color-ink-4)] mb-1">
        {title}
      </p>
      <dl className="space-y-2 text-sm">
        {items.map((w) => (
          <ScheduleRow
            key={w.filename}
            label={w.name}
            cron={w.schedules.map((s) => s.cron).join(', ')}
            human={w.schedules.map((s) => s.human).join(' · ')}
            note={w.description}
          />
        ))}
      </dl>
    </div>
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
        <dt className="text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)] font-medium">
          {label}
        </dt>
        <dd className="font-mono text-[0.65rem] text-[var(--color-ink-4)] tabular-nums shrink-0">
          {cron}
        </dd>
      </div>
      <p className="mt-0.5 font-mono tabular-nums text-[var(--color-ink)]">
        {human}
      </p>
      <p className="mt-1 text-xs text-[var(--color-ink-3)] leading-snug line-clamp-3">
        {note}
      </p>
    </div>
  );
}
