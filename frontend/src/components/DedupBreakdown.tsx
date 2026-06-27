import { Link } from 'react-router-dom';

import type { AuditRung } from '@/lib/types';
import { settingsDeepLink } from '@/lib/settingsAnchor';

/* The auditable breakdown of one dedup decision: one rung per signal (pHash / cosine /
 * forensic verdict / floor-plan / address), each showing the measured value against the
 * bar it was judged on, whether it was MET (copper ✓), NOT met (brick ✕) or informational
 * (• — e.g. cosine, which routes a model but never gates the merge), plus a deep-link to
 * the exact Settings knob that governs it. Computed server-side from the stored factor
 * detail, so it reads identically on the history feed and the review queue. Civic-archive:
 * copper = met, brick = unmet, borders-only, tabular/mono numerals. */

const STATUS: Record<string, { mark: string; cls: string; title: string }> = {
  met: {
    mark: '✓',
    cls: 'text-[var(--color-copper)]',
    title: 'Práh splněn',
  },
  unmet: {
    mark: '✕',
    cls: 'text-[var(--color-brick)]',
    title: 'Práh nesplněn / rozhodlo o zamítnutí',
  },
  info: {
    mark: '•',
    cls: 'text-[var(--color-ink-4)]',
    title: 'Informativní (nerozhoduje o sloučení samo o sobě)',
  },
};

// Strip the registry prefix so a deep-link reads as the knob's short name.
function shortKey(k: string): string {
  return k.replace(/^dedup_/, '').replace(/^llm_/, '').replace(/_/g, ' ');
}

export default function DedupBreakdown({ rungs }: { rungs: AuditRung[] | undefined }) {
  if (!rungs || rungs.length === 0) return null;
  return (
    <ul className="flex flex-col gap-1 rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-2.5 py-1.5">
      {rungs.map((r) => {
        const s = STATUS[r.status] ?? STATUS.info;
        const hasThreshold = r.threshold != null && r.threshold !== '';
        return (
          <li
            key={r.key}
            className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-[0.74rem] leading-snug"
          >
            <span
              className={`shrink-0 font-mono ${s.cls}`}
              title={s.title}
              aria-label={s.title}
            >
              {s.mark}
            </span>
            <span className="shrink-0 text-[var(--color-ink-2)]">{r.label}</span>
            <span className="font-mono tabular-nums text-[var(--color-ink)]">
              {String(r.value)}
              {hasThreshold ? ` ${r.comparator ?? ''} ${r.threshold}` : ''}
            </span>
            {r.settings_keys.map((k) => (
              <Link
                key={k}
                to={settingsDeepLink(k)}
                title={`Otevřít nastavení: ${k}`}
                className="shrink-0 inline-flex items-center gap-0.5 text-[0.68rem] text-[var(--color-ink-4)] hover:text-[var(--color-copper)] underline decoration-dotted underline-offset-2"
              >
                <span aria-hidden>⚙</span>
                {shortKey(k)}
              </Link>
            ))}
            {r.note && (
              <span className="basis-full text-[0.68rem] text-[var(--color-ink-4)] italic">
                {r.note}
              </span>
            )}
          </li>
        );
      })}
    </ul>
  );
}
