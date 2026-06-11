import { useState, type MouseEvent, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { useQuery, useQueries } from '@tanstack/react-query';
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
  fetchCategoryTrends,
  fetchHealthSummary,
  fetchImageStorageOverview,
  fetchImagesFailureOverview,
  fetchPortalHealth,
  fetchRecentScrapeRuns,
  fetchScraperHealthChecks,
} from '@/lib/queries';
import type {
  CategoryTrend,
  CategoryTrendPoint,
  HealthSummary,
  HealthSnapBucket,
  HealthFreshnessRow,
  HealthFailureRow,
  HealthCheckStatus,
  ImageFailureRow,
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
import { portalShort } from '@/lib/portals';
import { WORKFLOW_DOCS } from '@/lib/workflowDocs.generated';

const STALE_HOURS_WARN = 36;
// The pg_cron loop refreshes the Health matviews every 10 min; 25 min of
// silence means it has missed two cycles — likely dead, not just slow.
const HEALTH_DATA_STALE_MIN = 25;

const CATEGORY_LABELS: Record<string, string> = {
  byt: 'Byty',
  dum: 'Domy',
  komercni: 'Komerční',
  pozemek: 'Pozemky',
  ostatni: 'Ostatní',
};

const TYPE_LABELS: Record<string, string> = {
  pronajem: 'pronájem',
  prodej: 'prodej',
};

function categoryLabel(c: { category_main: string; category_type: string }): string {
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

      {data && <StaleHealthDataBanner generatedAt={data.generated_at ?? null} />}
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
  const imageFailuresQuery = useQuery<ImageFailureRow[], Error>({
    queryKey: ['images-failure-overview'],
    queryFn: fetchImagesFailureOverview,
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  return (
    <div className="mt-5 space-y-6">
      <PortalLedger />

      <section>
        <SectionHeading>Activity &amp; data quality</SectionHeading>
        <div className="mt-3 space-y-4">
          <Card label="Recent scrapes · last 14 d · all portals">
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
            <Card label="Snapshot density">
              <SnapshotBars rows={data.snapshot_density} totalListings={data.active_now} />
            </Card>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <Card label="Image failures">
              <ImageFailuresPanel
                rows={imageFailuresQuery.data}
                isLoading={imageFailuresQuery.isLoading}
                error={imageFailuresQuery.error}
              />
            </Card>
            <Card label="Freshness checks · last 24 h">
              <FreshnessRows rows={data.freshness_24h} />
            </Card>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <Card label="Fetch failures · top 10 by attempts">
              <FailuresPanel
                given_up={data.failures_given_up}
                total={data.failures_total}
                top10={data.failures_top10}
              />
            </Card>
          </div>
        </div>
      </section>
    </div>
  );
}

function SectionHeading({ children }: { children: ReactNode }) {
  return (
    <h2 className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
      {children}
    </h2>
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

type RollupStatus = HealthCheckStatus | 'idle' | 'loading';

function worstStatus(checks: ScraperHealthCheck[]): HealthCheckStatus {
  if (checks.some((c) => c.status === 'fail')) return 'fail';
  if (checks.some((c) => c.status === 'warn')) return 'warn';
  return 'pass';
}

const ROLLUP_DOT: Record<RollupStatus, string> = {
  pass: 'var(--color-sage)',
  warn: 'var(--color-ochre)',
  fail: 'var(--color-brick)',
  idle: 'var(--color-ink-4)',
  loading: 'var(--color-ink-4)',
};

const ROLLUP_LABEL: Record<RollupStatus, string> = {
  pass: 'Healthy', warn: 'Watch', fail: 'Problem', idle: 'Not started', loading: 'Checking…',
};

interface PortalGroup {
  key: string;
  label: string;
  scraper?: PortalHealth;
  parser?: PortalHealth;
}

/* A scraper facet is "active" (worth fetching checks for) once it has any
 * listings or runs; a planned pilot with neither reads idle, not false-red. */
function scraperHasActivity(p: PortalHealth): boolean {
  return p.listings_total > 0 || p.runs_7d > 0;
}

function portalHost(url: string | null): string | null {
  if (!url) return null;
  try { return new URL(url).host.replace(/^www\./, ''); } catch { return url; }
}

/* Group registry rows by canonical portal identity (home host), so a portal's
 * scraper + on-demand-parser facets fold into one card — which dedupes the two
 * "iDNES Reality" rows (scraper pilot + parser) the flat grid showed twice. */
function groupPortals(portals: PortalHealth[]): PortalGroup[] {
  const groups = new Map<string, PortalGroup>();
  for (const p of portals) {
    const key = portalHost(p.home_url) ?? p.source;
    let g = groups.get(key);
    if (!g) {
      g = { key, label: p.label };
      groups.set(key, g);
    }
    if (p.kind === 'scraper') { g.scraper = p; g.label = p.label; }
    else g.parser = p;
  }
  const rank = (g: PortalGroup): number =>
    g.scraper?.stage === 'live' ? 0 : g.scraper ? 1 : 2;
  return [...groups.values()].sort(
    (a, b) => rank(a) - rank(b) || a.label.localeCompare(b.label),
  );
}

type ChecksState =
  | { data?: ScraperHealthChecks; isLoading: boolean; error: Error | null }
  | undefined;

function PortalLedger() {
  const portalsQuery = useQuery<PortalHealth[], Error>({
    queryKey: ['portal-health'],
    queryFn: fetchPortalHealth,
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
  const portals = portalsQuery.data ?? [];
  const groups = groupPortals(portals);

  // One checks query per active scraper source — drives the roll-up dot even
  // while collapsed, so a problem is visible without expanding.
  const activeSources = portals
    .filter((p) => p.kind === 'scraper' && scraperHasActivity(p))
    .map((p) => p.source);
  const checkResults = useQueries({
    queries: activeSources.map((src) => ({
      queryKey: ['scraper-health-checks', src],
      queryFn: () => fetchScraperHealthChecks(src),
      refetchInterval: 60_000,
      staleTime: 30_000,
    })),
  });
  const checksBySource = new Map<string, ChecksState>();
  activeSources.forEach((src, i) => {
    const r = checkResults[i];
    checksBySource.set(src, { data: r.data, isLoading: r.isLoading, error: (r.error as Error) ?? null });
  });

  const scrapers = portals.filter((p) => p.kind === 'scraper').length;
  const parsers = portals.filter((p) => p.kind === 'parser').length;

  return (
    <section>
      <div className="flex items-baseline justify-between gap-4">
        <SectionHeading>Data sources</SectionHeading>
        {portals.length > 0 && (
          <span className="text-[0.65rem] text-[var(--color-ink-4)] tabular-nums">
            {scrapers} scraper{scrapers === 1 ? '' : 's'} · {parsers} on-demand parser
            {parsers === 1 ? '' : 's'}
          </span>
        )}
      </div>
      <div className="mt-3 space-y-3">
        {portalsQuery.error ? (
          <p className="text-sm text-[var(--color-brick)]">
            portal_health_summary failed: {portalsQuery.error.message}
          </p>
        ) : portalsQuery.isLoading && portals.length === 0 ? (
          <p className="text-sm text-[var(--color-ink-3)]">Loading sources…</p>
        ) : groups.length === 0 ? (
          <p className="text-sm text-[var(--color-ink-4)]">No portals registered.</p>
        ) : (
          groups.map((g) => (
            <PortalGroupCard
              key={g.key}
              group={g}
              checks={g.scraper ? checksBySource.get(g.scraper.source) : undefined}
            />
          ))
        )}
      </div>
    </section>
  );
}

function PortalGroupCard({
  group,
  checks,
}: {
  group: PortalGroup;
  checks: ChecksState;
}) {
  const [open, setOpen] = useState(false);
  const { scraper, parser } = group;

  let status: RollupStatus;
  if (scraper && scraperHasActivity(scraper)) {
    status = checks?.data ? worstStatus(checks.data.checks) : checks?.isLoading ? 'loading' : 'idle';
  } else if (scraper) {
    status = 'idle';
  } else {
    status = parser?.last_parsed_at ? 'pass' : 'idle';
  }

  const counts = checks?.data
    ? checks.data.checks.reduce(
        (a, c) => ({ ...a, [c.status]: a[c.status] + 1 }),
        { pass: 0, warn: 0, fail: 0 } as Record<HealthCheckStatus, number>,
      )
    : null;

  // Lead with the scraper's active count; but if the scraper facet is idle
  // and the parser has activity (e.g. iDNES: pilot scraper + live parser),
  // lead with the parser metric so the card doesn't read as dead.
  const headline =
    scraper && scraperHasActivity(scraper)
      ? { value: scraper.listings_active, label: 'active listings', has: scraper.listings_active > 0 }
      : parser && parser.parses_total > 0
        ? { value: parser.parses_total, label: 'URLs parsed', has: true }
        : scraper
          ? { value: scraper.listings_active, label: 'active listings', has: false }
          : { value: parser?.parses_total ?? 0, label: 'URLs parsed', has: (parser?.parses_total ?? 0) > 0 };

  return (
    <section className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full text-left px-4 py-3.5 flex items-center gap-4 hover:bg-[var(--color-paper-3)]/40 transition-colors"
        aria-expanded={open}
      >
        <span
          className="shrink-0 h-2.5 w-2.5 rounded-full"
          style={{ backgroundColor: ROLLUP_DOT[status] }}
          title={ROLLUP_LABEL[status]}
        />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-display text-lg leading-tight text-[var(--color-ink)] truncate">
              {group.label}
            </span>
            {scraper && <FacetChip kind="scraper" stage={scraper.stage} />}
            {parser && <FacetChip kind="parser" stage={parser.stage} />}
          </div>
          <div className="mt-1 flex items-center gap-x-5 gap-y-1 flex-wrap">
            {scraper ? (
              <>
                <Inline label="new 7d" value={fmtCount(scraper.scraped_new_7d)} />
                <Inline label="runs 7d" value={fmtCount(scraper.runs_7d)} />
                <Inline label="last scrape" value={scraper.last_scrape_at ? fmtRelative(scraper.last_scrape_at) : '—'} />
                {parser && <Inline label="parsed 30d" value={fmtCount(parser.parses_30d)} />}
              </>
            ) : parser ? (
              <>
                <Inline label="parsed 30d" value={fmtCount(parser.parses_30d)} />
                <Inline label="total" value={fmtCount(parser.parses_total)} />
                <Inline label="last parse" value={parser.last_parsed_at ? fmtRelative(parser.last_parsed_at) : '—'} />
              </>
            ) : null}
          </div>
        </div>
        <div className="shrink-0 text-right">
          <p
            className="font-mono tabular-nums text-[1.6rem] leading-none tracking-tight"
            style={{ color: headline.has ? 'var(--color-ink)' : 'var(--color-ink-4)' }}
          >
            {headline.has ? fmtCount(headline.value) : '—'}
          </p>
          <p className="mt-0.5 text-[0.6rem] text-[var(--color-ink-4)] tracking-wide">{headline.label}</p>
        </div>
        <div className="shrink-0 flex items-center gap-3">
          {counts && (
            <span className="text-[0.62rem] tabular-nums hidden sm:inline">
              {counts.fail > 0 && <span style={{ color: 'var(--color-brick)' }}>{counts.fail} problem </span>}
              {counts.warn > 0 && <span style={{ color: 'var(--color-ochre)' }}>{counts.warn} watch </span>}
              <span style={{ color: 'var(--color-sage)' }}>{counts.pass} ok</span>
            </span>
          )}
          <span className="text-[var(--color-ink-3)] font-mono text-xs w-3">{open ? '▾' : '▸'}</span>
        </div>
      </button>

      {open && (
        <div className="border-t border-[var(--color-rule-soft)] px-4 py-3 space-y-2">
          {scraper && scraperHasActivity(scraper) ? (
            <>
              <Disclosure label="Listings by category · reconciliation">
                <CategoryTable source={scraper.source} stage={scraper.stage} />
              </Disclosure>
              <Disclosure
                label="Scrape health checks"
                status={status === 'pass' || status === 'warn' || status === 'fail' ? status : undefined}
              >
                <HealthChecksPanel
                  checks={checks?.data}
                  isLoading={checks?.isLoading ?? false}
                  error={checks?.error ?? null}
                />
              </Disclosure>
            </>
          ) : scraper ? (
            <p className="text-sm text-[var(--color-ink-4)] px-1 py-2">
              Pipeline not started yet — no listings or runs recorded for this portal.
            </p>
          ) : null}

          {parser && (
            <Disclosure label="On-demand parser">
              <ParserFacetDetail parser={parser} />
            </Disclosure>
          )}

          <Disclosure label="Pipeline schedule">
            <PortalSchedule source={scraper?.source ?? null} />
          </Disclosure>
        </div>
      )}
    </section>
  );
}

function FacetChip({ kind, stage }: { kind: PortalKind; stage: PortalStage }) {
  return (
    <span className="inline-flex items-center px-1.5 py-0.5 rounded-[var(--radius-xs)] bg-[var(--color-rule-soft)] text-[0.55rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)] whitespace-nowrap">
      {PORTAL_KIND_LABEL[kind]} · {PORTAL_STAGE_LABEL[stage]}
    </span>
  );
}

function Inline({ label, value }: { label: string; value: ReactNode }) {
  return (
    <span className="inline-flex items-baseline gap-1.5">
      <span className="text-[0.55rem] tracking-[0.1em] uppercase text-[var(--color-ink-4)]">{label}</span>
      <span className="font-mono tabular-nums text-[0.72rem] text-[var(--color-ink-2)]">{value}</span>
    </span>
  );
}

function Disclosure({
  label,
  status,
  children,
}: {
  label: string;
  status?: HealthCheckStatus;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border border-[var(--color-rule-soft)] rounded-[var(--radius-sm)] bg-[var(--color-paper)]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full text-left px-3 py-2 flex items-center gap-2 hover:bg-[var(--color-paper-2)]"
        aria-expanded={open}
      >
        <span className="text-[var(--color-ink-4)] font-mono text-[0.7rem] w-3">{open ? '▾' : '▸'}</span>
        <span className="text-[0.68rem] tracking-[0.12em] uppercase text-[var(--color-ink-3)] font-medium flex-1">
          {label}
        </span>
        {status && (
          <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: ROLLUP_DOT[status] }} />
        )}
      </button>
      {open && <div className="px-3 pb-3 pt-1">{children}</div>}
    </div>
  );
}

function ParserFacetDetail({ parser }: { parser: PortalHealth }) {
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
      <PortalStat label="URLs parsed" value={fmtCount(parser.parses_total)} />
      <PortalStat label="parsed 30&thinsp;d" value={fmtCount(parser.parses_30d)} />
      <PortalStat
        label="last parse"
        value={parser.last_parsed_at ? fmtRelative(parser.last_parsed_at) : '—'}
        title={parser.last_parsed_at ? fmtAbsolute(parser.last_parsed_at) : undefined}
      />
    </div>
  );
}

function PortalSchedule({ source }: { source: string | null }) {
  const docs = WORKFLOW_DOCS
    .filter((w) => w.portal === source && w.schedules.length > 0)
    .sort((a, b) => a.name.localeCompare(b.name));
  if (docs.length === 0) {
    return (
      <p className="text-sm text-[var(--color-ink-4)]">
        No scheduled jobs — manual dispatch only, or not wired to a cron yet.
      </p>
    );
  }
  return (
    <dl className="space-y-2 text-sm">
      {docs.map((w) => (
        <ScheduleRow
          key={w.filename}
          label={w.name}
          cron={w.schedules.map((s) => s.cron).join(', ')}
          human={w.schedules.map((s) => s.human).join(' · ')}
          note={w.description}
        />
      ))}
    </dl>
  );
}

/* (PortalsSection / PortalCard replaced by PortalLedger / PortalGroupCard above.) */

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
/* Stale health-data warning (migration 176 refresh stamp)                    */
/*                                                                            */
/* The dashboard's numbers come from pg_cron-refreshed matviews. If the cron  */
/* loop dies, the page keeps serving old numbers that LOOK fresh — this is    */
/* the only signal that the data itself has stopped moving. Absent            */
/* generated_at (pre-176 payload) renders nothing.                            */
/* -------------------------------------------------------------------------- */

function StaleHealthDataBanner({ generatedAt }: { generatedAt: string | null }) {
  if (!generatedAt) return null;
  const ageMin = (Date.now() - new Date(generatedAt).getTime()) / 60_000;
  if (ageMin < HEALTH_DATA_STALE_MIN) return null;
  return (
    <div className="mt-4 p-3 rounded-[var(--radius-sm)] border border-[var(--color-ochre)]/40 bg-[var(--color-ochre-soft)] text-sm text-[var(--color-ochre)] flex items-baseline gap-2">
      <span className="text-[0.7rem] tracking-[0.18em] uppercase font-medium">stale</span>
      <span>
        Health data je zastaralá (pg_cron refresh možná stojí) — poslední refresh před{' '}
        <span className="font-mono tabular-nums">{Math.round(ageMin)}&thinsp;min</span>.
      </span>
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
  source,
  stage,
}: {
  source: string;
  stage?: PortalStage;
}) {
  const [grain, setGrain] = useState<'hour' | 'day'>('hour');

  // One source-scoped RPC (migration 119) supplies the whole table: per-category
  // totals/active/new/flipped/failures, the latest run's portal+collected, and
  // the hourly/daily portal-vs-DB trend series. Already sorted by active desc.
  const trendsQuery = useQuery({
    queryKey: ['category-trends', source],
    queryFn: () => fetchCategoryTrends(source),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
  const cats = trendsQuery.data ?? [];
  const isPilot = stage === 'pilot';

  if (trendsQuery.isLoading && cats.length === 0) {
    return <p className="text-sm text-[var(--color-ink-3)]">Loading categories…</p>;
  }
  if (trendsQuery.error) {
    return (
      <p className="text-sm text-[var(--color-brick)]">
        category_trends failed: {(trendsQuery.error as Error).message}
      </p>
    );
  }
  if (cats.length === 0) {
    return <p className="text-sm text-[var(--color-ink-4)]">No per-category data recorded yet for this portal.</p>;
  }

  return (
    <div>
      <p className="mb-2 text-xs text-[var(--color-ink-3)] leading-snug">
        <span className="text-[var(--color-ink-2)]">total</span> = every listing we hold
        (active + delisted); <span className="text-[var(--color-ink-2)]">portal</span> = the
        portal&rsquo;s reported active total at the last index walk;{' '}
        <span className="text-[var(--color-ink-2)]">index</span> = share of those the walk
        collected; <span className="text-[var(--color-ink-2)]">queue</span> = seen but not yet
        fetched by the detail-drain. Trend overlays{' '}
        <span style={{ color: 'var(--color-copper)' }}>active on portal</span> vs{' '}
        <span style={{ color: 'var(--color-ink-2)' }}>active in DB</span>
        {isPilot ? <>. Pilot portals walk a partial index, so delistings aren&rsquo;t inferred.</> : null}.
      </p>
      <div className="overflow-x-auto -mx-1">
        <table className="w-full text-xs">
          <thead className="text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
            <tr>
              <th className="text-left  py-1.5 px-1.5 font-medium">Category</th>
              <th className="text-right py-1.5 px-1.5 font-medium">Total</th>
              <th className="text-right py-1.5 px-1.5 font-medium">Active</th>
              <th className="text-right py-1.5 px-1.5 font-medium">Portal</th>
              <th className="text-right py-1.5 px-1.5 font-medium">Index</th>
              <th className="text-right py-1.5 px-1.5 font-medium">Queue</th>
              <th className="text-right py-1.5 px-1.5 font-medium">new&nbsp;t&thinsp;/&thinsp;7d</th>
              <th className="text-right py-1.5 px-1.5 font-medium">flipped&nbsp;t&thinsp;/&thinsp;7d</th>
              <th className="text-right py-1.5 px-1.5 font-medium">failed</th>
              <th className="text-left  py-1.5 px-1.5 font-medium align-top">
                <div className="flex items-center justify-between gap-2">
                  <span>trend</span>
                  <GrainToggle grain={grain} onChange={setGrain} />
                </div>
                <div className="mt-1 flex items-center gap-2.5 normal-case tracking-normal text-[0.6rem] text-[var(--color-ink-3)]">
                  <span className="inline-flex items-center gap-1">
                    <span className="inline-block rounded-full" style={{ width: 12, height: 2, background: 'var(--color-copper)' }} />
                    Portal
                  </span>
                  <span className="inline-flex items-center gap-1">
                    <span className="inline-block rounded-full" style={{ width: 12, height: 2, background: 'var(--color-ink-2)' }} />
                    In&nbsp;DB
                  </span>
                </div>
              </th>
            </tr>
          </thead>
          <tbody>
            {cats.map((c) => (
              <CategoryTableRow
                key={`${c.category_main}-${c.category_type}`}
                row={c}
                grain={grain}
              />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function GrainToggle({
  grain,
  onChange,
}: {
  grain: 'hour' | 'day';
  onChange: (g: 'hour' | 'day') => void;
}) {
  return (
    <span className="inline-flex rounded-[var(--radius-sm)] border border-[var(--color-rule)] overflow-hidden normal-case tracking-normal">
      {(['hour', 'day'] as const).map((g) => (
        <button
          key={g}
          type="button"
          onClick={() => onChange(g)}
          className="px-1.5 py-0.5 text-[0.6rem] font-medium transition-colors"
          style={
            grain === g
              ? { background: 'var(--color-copper)', color: 'var(--color-paper-3)' }
              : { color: 'var(--color-ink-3)' }
          }
        >
          {g === 'hour' ? 'Hour' : 'Day'}
        </button>
      ))}
    </span>
  );
}

function TodayWindowCell({
  today,
  window,
  accent,
}: {
  today: number;
  window: number;
  accent: string;
}) {
  return (
    <td className="py-1.5 px-1.5 text-right font-mono tabular-nums whitespace-nowrap">
      <span style={{ color: today > 0 ? accent : 'var(--color-ink-3)' }}>{fmtCount(today)}</span>
      <span className="text-[var(--color-ink-4)]"> / {fmtCount(window)}</span>
    </td>
  );
}

function CategoryTableRow({
  row,
  grain,
}: {
  row: CategoryTrend;
  grain: 'hour' | 'day';
}) {
  const failuresActive = row.failures_total - row.failures_given_up;
  const portalTotal = row.portal_total;
  const collected = row.collected;
  const indexPct =
    portalTotal && portalTotal > 0 && collected != null
      ? (collected / portalTotal) * 100
      : null;
  const indexColour =
    indexPct == null
      ? 'var(--color-ink-4)'
      : indexPct >= 99
        ? 'var(--color-sage)'
        : indexPct >= 95
          ? 'var(--color-ochre)'
          : 'var(--color-brick)';
  // Detail-drain backlog proxy: seen (collected, or the portal total) minus
  // what is currently active.
  const seen = collected ?? portalTotal;
  const queue = seen != null ? Math.max(0, seen - row.active_now) : null;

  const trendPoints = grain === 'hour' ? row.hourly : row.daily;

  return (
    <tr className="border-t border-[var(--color-rule-soft)] hover:bg-[var(--color-paper-3)]/40">
      <td className="py-1.5 px-1.5 text-[var(--color-ink)] whitespace-nowrap">
        {categoryLabel(row)}
      </td>
      <td className="py-1.5 px-1.5 text-right font-mono tabular-nums text-[var(--color-ink-2)]">
        {fmtCount(row.total_in_db)}
      </td>
      <td className="py-1.5 px-1.5 text-right font-mono tabular-nums text-[var(--color-ink)]">
        {fmtCount(row.active_now)}
      </td>
      <td className="py-1.5 px-1.5 text-right font-mono tabular-nums text-[var(--color-ink-2)]">
        {portalTotal != null ? fmtCount(portalTotal) : '—'}
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
      <TodayWindowCell today={row.new_today} window={row.new_7d} accent="var(--color-copper)" />
      <TodayWindowCell today={row.flipped_today} window={row.flipped_7d} accent="var(--color-brick)" />
      <td className="py-1.5 px-1.5 text-right font-mono tabular-nums leading-tight">
        <span style={{ color: failuresActive > 0 ? 'var(--color-ochre)' : 'var(--color-ink)' }}>
          {fmtCount(failuresActive)}
        </span>
        {row.failures_given_up > 0 && (
          <span className="block text-[0.6rem] text-[var(--color-brick)]">
            {fmtCount(row.failures_given_up)}&thinsp;given&nbsp;up
          </span>
        )}
      </td>
      <td className="py-1.5 px-1.5">
        <TrendChart points={trendPoints} grain={grain} />
      </td>
    </tr>
  );
}

// Tooltip date formats for the trend hover: full timestamp for the hourly
// series, just the day for the daily one. Defined here (not reusing the later
// CHART_TIME_FMT) so the trend feature is self-contained.
const TREND_HOUR_FMT = new Intl.DateTimeFormat('cs-CZ', {
  timeZone: 'Europe/Prague',
  day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
});
const TREND_DAY_FMT = new Intl.DateTimeFormat('cs-CZ', {
  timeZone: 'Europe/Prague',
  day: '2-digit', month: '2-digit',
});

// Two-line sparkline on a shared auto-fit scale: copper = active on portal,
// ink = active in DB. Auto-fit (not zero-based) so the gap between the two —
// the real drift — stays visible even when both sit near the same magnitude.
// Hovering snaps to the nearest sample and shows both series' values + the gap
// at that point; the tooltip is fixed-positioned so the narrow, horizontally
// scrollable table cell can never clip it.
function TrendChart({
  points,
  grain,
  width = 116,
  height = 26,
}: {
  points: CategoryTrendPoint[];
  grain: 'hour' | 'day';
  width?: number;
  height?: number;
}) {
  const [hover, setHover] = useState<{ i: number; x: number; y: number } | null>(null);
  const pts = points.filter((p) => p.portal != null || p.db != null);
  if (pts.length === 0) {
    return <span className="text-[0.65rem] text-[var(--color-ink-4)]">no data</span>;
  }
  const vals = pts.flatMap((p) =>
    [p.portal, p.db].filter((v): v is number => v != null),
  );
  const max = Math.max(...vals);
  const min = Math.min(...vals);
  const span = max - min || 1;
  const stepX = pts.length > 1 ? width / (pts.length - 1) : width;
  const xFor = (i: number) => (pts.length > 1 ? i * stepX : width / 2);
  const yFor = (v: number) => height - ((v - min) / span) * (height - 3) - 1.5;
  const lineFor = (key: 'portal' | 'db') =>
    pts
      .map((p, i) => {
        const v = p[key];
        if (v == null) return null;
        return `${xFor(i).toFixed(1)},${yFor(v).toFixed(1)}`;
      })
      .filter((s): s is string => s != null)
      .join(' ');

  const onMove = (e: MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const localX = ((e.clientX - rect.left) / (rect.width || width)) * width;
    const i = Math.min(pts.length - 1, Math.max(0, Math.round(localX / stepX)));
    setHover({ i, x: e.clientX, y: e.clientY });
  };

  const hp = hover ? pts[hover.i] : null;
  const hx = hover ? xFor(hover.i) : 0;

  return (
    <span className="relative inline-block">
      <svg
        width={width}
        height={height}
        className="flex-shrink-0 block cursor-crosshair"
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
      >
        <polyline
          fill="none"
          stroke="var(--color-ink-2)"
          strokeWidth="1.25"
          strokeLinecap="round"
          strokeLinejoin="round"
          points={lineFor('db')}
        />
        <polyline
          fill="none"
          stroke="var(--color-copper)"
          strokeWidth="1.25"
          strokeLinecap="round"
          strokeLinejoin="round"
          points={lineFor('portal')}
        />
        {hp && (
          <g>
            <line
              x1={hx} x2={hx} y1={0} y2={height}
              stroke="var(--color-ink-3)" strokeWidth="0.75" strokeDasharray="2 2"
            />
            {hp.db != null && (
              <circle cx={hx} cy={yFor(hp.db)} r="2.1" fill="var(--color-ink-2)" />
            )}
            {hp.portal != null && (
              <circle cx={hx} cy={yFor(hp.portal)} r="2.1" fill="var(--color-copper)" />
            )}
          </g>
        )}
      </svg>
      {hp && hover && (
        <div
          className="fixed z-50 pointer-events-none rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-2 py-1 text-[0.65rem] leading-tight shadow-sm min-w-[7.5rem]"
          style={{ left: hover.x + 12, top: hover.y + 12 }}
        >
          <div className="mb-0.5 tabular-nums text-[var(--color-ink-3)]">
            {(grain === 'hour' ? TREND_HOUR_FMT : TREND_DAY_FMT).format(new Date(hp.t))}
          </div>
          <div className="flex items-center justify-between gap-3 tabular-nums">
            <span style={{ color: 'var(--color-copper)' }}>Portal</span>
            <span className="font-mono text-[var(--color-ink)]">
              {hp.portal != null ? fmtCount(hp.portal) : '—'}
            </span>
          </div>
          <div className="flex items-center justify-between gap-3 tabular-nums">
            <span style={{ color: 'var(--color-ink-2)' }}>In&nbsp;DB</span>
            <span className="font-mono text-[var(--color-ink)]">
              {hp.db != null ? fmtCount(hp.db) : '—'}
            </span>
          </div>
          {hp.portal != null && hp.db != null && (
            <div className="mt-0.5 flex items-center justify-between gap-3 border-t border-[var(--color-rule-soft)] pt-0.5 tabular-nums">
              <span className="text-[var(--color-ink-3)]">gap</span>
              <span className="font-mono text-[var(--color-ink-2)]">
                {hp.db - hp.portal > 0 ? '+' : ''}{fmtCount(hp.db - hp.portal)}
              </span>
            </div>
          )}
        </div>
      )}
    </span>
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
      images_found: r.images_discovered,
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
              dataKey="images_found"
              name="images found"
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
              <th
                className="text-right py-1.5 px-1.5 font-medium"
                title="image-URL rows this run recorded. Bytes are uploaded to R2 asynchronously by the image drain — see R2 coverage in the Image mirror tile."
              >
                Imgs found
              </th>
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
/* Image-download failures (images_failure_overview RPC, migration 177)        */
/*                                                                            */
/* The Image mirror shows stored-vs-total; this card shows WHY the gap exists  */
/* — terminally-classified URLs (unavailable_reason), silently-exhausted       */
/* retries, and the coarse HTTP-error class of what is still pending.          */
/* -------------------------------------------------------------------------- */

const FAILURE_BUCKET_COLOUR: Record<string, string> = {
  pending: 'var(--color-ink-2)',
  exhausted: 'var(--color-brick)',
  unavailable: 'var(--color-ochre)',
};

function ImageFailuresPanel({
  rows,
  isLoading,
  error,
}: {
  rows: ImageFailureRow[] | undefined;
  isLoading: boolean;
  error: Error | null;
}) {
  if (error) {
    return (
      <p className="text-sm text-[var(--color-brick)]">
        images_failure_overview failed: {error.message}
      </p>
    );
  }
  if (isLoading && !rows) {
    return <p className="text-sm text-[var(--color-ink-4)]">Loading…</p>;
  }
  if (!rows || rows.length === 0) {
    return (
      <p className="text-sm text-[var(--color-ink-4)]">
        No data yet — the rollup refreshes with the 2-hourly image drain.
      </p>
    );
  }

  const totals = rows.reduce(
    (acc, r) => {
      if (r.bucket !== 'stored') acc[r.bucket] = (acc[r.bucket] ?? 0) + r.n;
      return acc;
    },
    {} as Record<string, number>,
  );
  // Breakdown: rows carrying a reason or error class (detail '') are the
  // never-attempted pending backlog — the Image mirror already covers those.
  const breakdown = rows
    .filter((r) => r.bucket !== 'stored' && r.detail !== '')
    .sort((a, b) => b.n - a.n)
    .slice(0, 12);

  return (
    <div>
      <div className="flex items-center gap-4 text-xs text-[var(--color-ink-2)]">
        {(['pending', 'exhausted', 'unavailable'] as const).map((bucket) => (
          <span key={bucket} className="inline-flex items-baseline gap-1.5">
            <span
              className="text-[0.62rem] tracking-[0.16em] uppercase"
              style={{ color: FAILURE_BUCKET_COLOUR[bucket] }}
            >
              {bucket}
            </span>
            <span className="font-mono tabular-nums text-[var(--color-ink)]">
              {fmtCount(totals[bucket] ?? 0)}
            </span>
          </span>
        ))}
      </div>

      {breakdown.length === 0 ? (
        <p className="mt-3 text-sm text-[var(--color-ink-4)]">
          No failure reasons recorded — the pending backlog has not been attempted yet.
        </p>
      ) : (
        <table className="w-full text-xs mt-3">
          <thead className="text-[0.6rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
            <tr>
              <th className="text-left  py-1.5 px-1.5 font-medium">Portal</th>
              <th className="text-left  py-1.5 px-1.5 font-medium">State</th>
              <th className="text-left  py-1.5 px-1.5 font-medium">Reason</th>
              <th className="text-right py-1.5 px-1.5 font-medium">Images</th>
            </tr>
          </thead>
          <tbody>
            {breakdown.map((r) => (
              <tr
                key={`${r.source}-${r.bucket}-${r.detail}`}
                className="border-t border-[var(--color-rule-soft)]"
              >
                <td className="py-1.5 px-1.5 text-[var(--color-ink)]">{portalShort(r.source)}</td>
                <td className="py-1.5 px-1.5" style={{ color: FAILURE_BUCKET_COLOUR[r.bucket] }}>
                  {r.bucket}
                </td>
                <td className="py-1.5 px-1.5 font-mono text-[var(--color-ink-2)]">{r.detail}</td>
                <td className="py-1.5 px-1.5 text-right font-mono tabular-nums text-[var(--color-ink)]">
                  {fmtCount(r.n)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Schedule rows (rendered per-portal by PortalSchedule, data-driven from       */
/* workflowDocs.generated.ts so the cron lines can never go stale)             */
/* -------------------------------------------------------------------------- */

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
