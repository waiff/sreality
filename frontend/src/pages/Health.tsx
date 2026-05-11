import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { fetchHealthSummary } from '@/lib/queries';
import type {
  HealthSummary,
  HealthDayCount,
  HealthSnapBucket,
  HealthFreshnessRow,
  HealthFailureRow,
  HealthCategoryBlock,
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
  return (
    <div className="mt-5 space-y-5">
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <LastScrapeTile lastScrapeAt={data.last_scrape_at} />
        {data.by_category.map((c) => (
          <CategoryTile key={`${c.category_main}-${c.category_type}`} block={c} />
        ))}
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
