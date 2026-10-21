/* Health: what is wrong with the platform, where and why, and the page that owns the fix
 * (docs/design/health-dashboard.md § 5, wave 8). ONE read, every 5 minutes while the tab is visible
 * and on focus (lib/healthChecks HEALTH_QUERY): the Watchers head the page, five tiles count, four
 * sections in a fixed order hold every check worst first, the portal matrix leading Portals. The
 * browser holds no threshold: every status is the view's `effective_status` or a check's own.
 *
 * A first read that fails or comes back EMPTY (not an admin, no registry) is an error with Retry,
 * never "all clear"; a failed refetch keeps its data and says so, error included, in a live region;
 * Not measured and stale never render OK. One page clock moves every "X ago" between polls.
 * #<key> and #<key>:<subject> land on their row or cell, scrolled to and focused, opening whatever
 * hides it. */
import { useEffect, useMemo, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import ErrorBanner from '@/components/ErrorBanner';
import Panel from '@/components/Panel';
import Skeleton from '@/components/Skeleton';
import Stat from '@/components/Stat';
import StatusDot from '@/components/StatusDot';
import CheckRow, { fmtNumber, fmtSince, measuredLine } from '@/components/health/CheckRow';
import PortalMatrix from '@/components/health/PortalMatrix';
import {
  HEALTH_QUERY,
  HealthEmptyError,
  LIVENESS_KEY,
  SECTIONS,
  anchorId,
  cellsWorstFirst,
  isOpen,
  parseHash,
  pivotByPortal,
  stripCounts,
  worstFirst,
  type HealthRow,
} from '@/lib/healthChecks';
import { fmtAbsolute, fmtRelative, fmtTime24 } from '@/lib/format';

type Target = { key: string; subject?: string } | null;
type Section = { domain: string; title: string; rows: HealthRow[] };

/* How often the page re-renders its "X ago" stamps (the read line, every row's "measured X ago"):
 * each is computed at render (lib/format fmtRelative), so one page tick moves them all between
 * polls and no row keeps a timer. A display cadence, not a threshold. */
const TICK_MS = 30_000;

function useClock(): void {
  const [, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(id);
  }, []);
}

/* Everything the page draws, derived once per read (the body is ~0.5 MB; never per render). A
 * check whose domain names no section still shows, under "Other checks", never silently dropped. */
function arrange(rows: HealthRow[]) {
  const known = new Set<string>(SECTIONS.map(([domain]) => domain));
  const sections: Section[] = SECTIONS.map(([domain, title]) => ({
    domain,
    title,
    rows: worstFirst(rows.filter((r) => r.domain === domain)),
  }));
  const other = worstFirst(rows.filter((r) => !known.has(r.domain)));
  if (other.length) sections.push({ domain: 'other', title: 'Other checks', rows: other });
  return {
    liveness: rows.find((r) => r.check_key === LIVENESS_KEY) ?? null,
    strip: stripCounts(rows),
    pivot: pivotByPortal(rows),
    sections,
  };
}

export default function Health() {
  const q = useQuery(HEALTH_QUERY);
  const location = useLocation();
  const target = useMemo(() => parseHash(location.hash), [location.hash]);
  const view = useMemo(() => (q.data ? arrange(q.data) : null), [q.data]);
  useScrollToTarget(target, view != null, location.key);
  useClock();

  return (
    <div className="mx-auto max-w-screen-xl px-4 pb-10 pt-5 md:px-6">
      <header className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2">
        <div className="min-w-0">
          <h1 className="text-2xl leading-tight">Health</h1>
          {view && <Watchers row={view.liveness} />}
        </div>
        {view && (
          <ReadStamp
            readAt={q.dataUpdatedAt}
            failedAt={q.isRefetchError ? q.errorUpdatedAt : null}
            error={q.error?.message}
            onRetry={() => q.refetch()}
          />
        )}
      </header>

      {q.isPending ? (
        <Loading />
      ) : !view ? (
        <ErrorBanner
          title={q.error instanceof HealthEmptyError ? 'Empty read:' : 'Failed to load:'}
          message={q.error?.message ?? 'The health view did not answer.'}
          onRetry={() => void q.refetch()}
          retryLabel="Retry"
        />
      ) : (
        <>
          <Strip strip={view.strip} />
          <div className="mt-5 space-y-5">
            {view.sections.map((s) => (
              <Panel
                key={s.domain}
                id={`section-${s.domain}`}
                title={s.title}
                lede={s.domain === 'other' ? 'Checks whose registry domain names no section of this page.' : undefined}
              >
                <SectionBody section={s} pivot={s.domain === 'portals' ? view.pivot : null} target={target} />
              </Panel>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

/* The watchers speak the page's first line: the liveness row's status and, when it is not OK,
 * its own cells' sentences (worker heartbeat, health lane, stale alarms, unalerted cells), worst
 * first, as written. A stale or absent liveness row reads Not measured, never OK. */
function Watchers({ row }: { row: HealthRow | null }) {
  if (!row) {
    return (
      <p className="mt-1 flex flex-wrap items-center gap-x-2 text-sm text-[var(--color-ink-2)]">
        <span>Watchers</span>
        <StatusDot status="unknown" />
        <span>· no watcher row: the registry holds no liveness check</span>
      </p>
    );
  }
  const st = row.effective_status;
  // Stale (or never run): the watcher itself is overdue, so its stored cells are history, withheld.
  // Otherwise only its non-OK cells are quoted and never its stored value, so a row stored
  // 'unknown' over OK cells (health_liveness, migration 633) draws no OK either.
  const overdue = row.run_at == null || (st === 'unknown' && row.status !== 'unknown');
  const cells = overdue ? [] : cellsWorstFirst(row.details?.cells ?? []).filter((c) => c.status !== 'ok');
  const when = overdue ? measuredLine(row) : { text: `checks measured ${fmtRelative(row.run_at)}`, title: fmtAbsolute(row.run_at) };
  const why = !overdue && st !== 'ok' && !cells.length ? row.details?.message : undefined;
  return (
    <div className="mt-1 text-sm text-[var(--color-ink-2)]">
      <p className="flex flex-wrap items-center gap-x-2">
        <span className="text-[var(--color-ink)]">Watchers</span>
        <StatusDot status={st} />
        <span className="font-mono text-[0.75rem] tabular-nums" title={when.title}>
          · {when.text}
        </span>
      </p>
      {why && <p className="mt-0.5 text-[0.8rem]">{why}</p>}
      {cells.length > 0 && (
        <ul className="mt-1 space-y-0.5 text-[0.8rem]">
          {cells.map((c, i) => (
            <li key={`${i}:${c.subject}`} className="flex flex-wrap items-baseline gap-x-2">
              <StatusDot status={c.status} size="sm" />
              <span className="font-mono text-[0.72rem]">{c.subject}</span>
              <span className="min-w-0 break-words">{c.message}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/* When the data on screen was read and, after a failed refetch, that it failed and why: the data
 * stays. The failure line is a live region mounted before any failure, so it is heard when it
 * fills; the read line is not live (it ticks) and takes focus after a Retry that read. */
function ReadStamp({
  readAt,
  failedAt,
  error,
  onRetry,
}: {
  readAt: number;
  failedAt: number | null;
  error?: string;
  onRetry: () => Promise<{ isError: boolean }>;
}) {
  const readLine = useRef<HTMLParagraphElement>(null);
  const read = new Date(readAt).toISOString();
  const retry = () =>
    void onRetry().then((r) => {
      if (!r.isError) readLine.current?.focus();
    });
  return (
    <div className="font-mono text-[0.72rem] tabular-nums text-[var(--color-ink-2)] md:text-right">
      <p ref={readLine} tabIndex={-1}>
        read <span title={fmtAbsolute(read)}>{fmtRelative(read)}</span> · refreshes every{' '}
        {HEALTH_QUERY.refetchInterval / 60_000}&nbsp;min
      </p>
      <p role="status" aria-live="polite" className="break-words text-[var(--color-brick)]">
        {failedAt != null && (
          <>
            refetch failed at {fmtTime24(new Date(failedAt).toISOString())}
            {error ? `: ${error}` : ''} —{' '}
            <button
              type="button"
              onClick={retry}
              className="font-medium underline underline-offset-2 hover:no-underline max-md:inline-flex max-md:min-h-11 max-md:items-center"
            >
              Retry
            </button>
          </>
        )}
      </p>
    </div>
  );
}

/* Five quiet counts; colour only in a dot, never in a number, and a dot only beside a count that
 * is not zero: a calm page draws no red. Problems and Watch count ALARM checks, Not measured every
 * check, Open incidents alarm incidents only (a standing gauge is not news); "worse than last week"
 * counts gauge cells, and is "not comparable" until a week of trend exists. The design's sixth tile
 * (where alerts reach) was dropped by ruling R2b (2026-10-21): alerts reach Health and the canary e-mail. */
function Strip({ strip }: { strip: ReturnType<typeof stripCounts> }) {
  const { openIncidents, gaugesWorse } = strip;
  return (
    <div role="group" aria-label="Summary" className="mt-5 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
      <Stat
        label="Open incidents"
        value={fmtNumber(openIncidents.count)}
        hint={openIncidents.oldest ? `alarms · oldest since ${fmtSince(openIncidents.oldest)}` : 'alarm incidents'}
      />
      <Stat label="Problems" status={strip.problems ? 'fail' : undefined} value={fmtNumber(strip.problems)} hint="alarm checks" />
      <Stat label="Watch" status={strip.watch ? 'warn' : undefined} value={fmtNumber(strip.watch)} hint="alarm checks" />
      <Stat
        label="Not measured"
        status={strip.notMeasured ? 'unknown' : undefined}
        value={fmtNumber(strip.notMeasured)}
        hint="all checks"
      />
      <Stat
        label="Gauges worse than last week"
        value={gaugesWorse == null ? '—' : fmtNumber(gaugesWorse)}
        hint={gaugesWorse == null ? 'not comparable yet' : 'gauge cells'}
      />
    </div>
  );
}

/* A section's rows, worst first. "Nothing open" only when every check in it reads OK and none has
 * an open incident: Not measured is never nothing. */
function SectionBody({
  section,
  pivot,
  target,
}: {
  section: Section;
  pivot: ReturnType<typeof pivotByPortal> | null;
  target: Target;
}) {
  const { rows } = section;
  if (!rows.length) {
    return <p className="text-sm text-[var(--color-ink-2)]">No check of the registry belongs to this section.</p>;
  }
  const calm = rows.every((r) => r.effective_status === 'ok' && !r.incidents.some(isOpen));
  return (
    <>
      {calm && (
        <p className="mb-2 flex items-center gap-2 text-sm text-[var(--color-ink-2)]">
          <StatusDot status="ok" word="sr-only" />
          Nothing open · <span className="font-mono tabular-nums">{rows.length}</span>{' '}
          {rows.length === 1 ? 'check' : 'checks'} measured
        </p>
      )}
      {pivot && <PortalMatrix pivot={pivot} />}
      <div className={`divide-y divide-[var(--color-rule-soft)] ${pivot ? 'mt-3 border-t border-[var(--color-rule)]' : ''}`}>
        {rows.map((r) => (
          <CheckRow
            key={r.check_key}
            row={r}
            target={target?.key === r.check_key ? { subject: target.subject } : undefined}
          />
        ))}
      </div>
      {section.domain === 'platform' && (
        <p className="mt-3 rounded-[var(--radius-sm)] border border-dashed border-[var(--color-rule-strong)] px-3 py-2 text-[0.78rem] text-[var(--color-ink-2)]">
          Not measured here: user-facing request errors and latency live in the Railway and Supabase logs.
        </p>
      )}
    </>
  );
}

function Loading() {
  return (
    <div role="status" className="mt-5 space-y-5">
      <span className="sr-only">Loading the health checks…</span>
      <Skeleton height={72} />
      <Skeleton height={364} />
      <Skeleton height={120} />
      <Skeleton height={160} />
    </div>
  );
}

/* A link lands on `/health#key` or `#key:subject` before the rows exist, so the browser's own jump
 * found nothing (and a router Link never makes one). Once per navigation, after the target rendered
 * (the row opens whatever hid it in the same commit), scroll to it and move focus there (the row and
 * an anchored cell take tabIndex -1), so the next Tab starts at the target, not back at the link.
 * The ref is set inside the frame so a cancelled frame (StrictMode's double effect) retries.
 * scrollIntoView is optional-called: jsdom has none. */
function useScrollToTarget(target: Target, ready: boolean, locationKey: string) {
  const done = useRef<string | null>(null);
  useEffect(() => {
    if (!target || !ready || done.current === locationKey) return;
    const frame = requestAnimationFrame(() => {
      const el =
        document.getElementById(anchorId(target.key, target.subject)) ?? document.getElementById(anchorId(target.key));
      if (!el) return;
      done.current = locationKey;
      el.scrollIntoView?.({ block: 'start' });
      el.focus({ preventScroll: true });
    });
    return () => cancelAnimationFrame(frame);
  }, [target, ready, locationKey]);
}
