import { useLayoutEffect, useRef, useState } from 'react';
import type {
  ListingSnapshotPublic,
  ListingFreshnessCheckPublic,
} from '@/lib/types';
import { fmtCzk, fmtAbsolute } from '@/lib/format';

const HEIGHT = 88;
const LANE_Y = 40;
const PAD_X = 48;
const DOT_R = 4.5;
const RING_R = 4;
const LABEL_MIN_PX = 60;
const DAY_MS = 86_400_000;

interface Props {
  firstSeenAt: string;
  lastSeenAt: string;
  isActive: boolean;
  snapshots: ListingSnapshotPublic[];
  freshnessChecks: ListingFreshnessCheckPublic[];
}

export default function SnapshotTimeline({
  firstSeenAt,
  lastSeenAt,
  isActive,
  snapshots,
  freshnessChecks,
}: Props) {
  const [width, setWidth] = useState(720);
  const containerRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    if (!containerRef.current) return;
    const el = containerRef.current;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width ?? 720;
      setWidth(Math.max(280, Math.round(w)));
    });
    ro.observe(el);
    setWidth(Math.max(280, el.clientWidth || 720));
    return () => ro.disconnect();
  }, []);

  const t = (iso: string): number => new Date(iso).getTime();
  const firstT = t(firstSeenAt);
  const lastSeenT = t(lastSeenAt);
  const nowT = Date.now();
  const snapTs = snapshots.map((s) => t(s.scraped_at));
  const checkTs = freshnessChecks.map((c) => t(c.checked_at));

  const startT = Math.min(firstT, ...snapTs, ...checkTs);
  const rightAnchorT = isActive ? Math.max(nowT, lastSeenT) : lastSeenT;
  const endT = Math.max(rightAnchorT, lastSeenT, ...snapTs, ...checkTs);
  const span = Math.max(endT - startT, DAY_MS);
  const usableW = Math.max(width - PAD_X * 2, 1);
  const x = (ms: number) => PAD_X + ((ms - startT) / span) * usableW;

  const ticks = computeTicks(startT, endT);

  const sortedSnaps = [...snapshots].sort((a, b) => t(a.scraped_at) - t(b.scraped_at));
  const snapXs = sortedSnaps.map((s) => x(t(s.scraped_at)));
  const labelMask = pickLabelMask(snapXs, LABEL_MIN_PX);

  const coverageStartX = x(firstT);
  const coverageEndX = x(lastSeenT);
  const showGap = rightAnchorT - lastSeenT > DAY_MS;

  return (
    <div ref={containerRef} className="w-full">
      <svg
        width="100%"
        height={HEIGHT}
        viewBox={`0 0 ${width} ${HEIGHT}`}
        role="img"
        aria-label="Snapshot timeline"
        style={{ display: 'block' }}
      >
        <line
          x1={PAD_X}
          x2={width - PAD_X}
          y1={LANE_Y}
          y2={LANE_Y}
          stroke="var(--color-rule-soft)"
          strokeWidth={2}
          strokeLinecap="round"
        />

        {ticks.map((tk, i) => (
          <line
            key={i}
            x1={x(tk.ms)}
            x2={x(tk.ms)}
            y1={LANE_Y - (tk.major ? 8 : 4)}
            y2={LANE_Y + (tk.major ? 8 : 4)}
            stroke="var(--color-rule)"
            strokeWidth={1}
          />
        ))}

        <line
          x1={coverageStartX}
          x2={coverageEndX}
          y1={LANE_Y}
          y2={LANE_Y}
          stroke="var(--color-copper)"
          strokeOpacity={0.32}
          strokeWidth={2}
          strokeLinecap="round"
        />

        {showGap && (
          <line
            x1={coverageEndX}
            x2={x(rightAnchorT)}
            y1={LANE_Y}
            y2={LANE_Y}
            stroke="var(--color-ink-3)"
            strokeOpacity={0.35}
            strokeWidth={1.5}
            strokeDasharray="2 3"
            strokeLinecap="round"
          />
        )}

        {freshnessChecks.map((c) => {
          const cx = x(t(c.checked_at));
          return (
            <circle
              key={`f-${c.id}`}
              cx={cx}
              cy={LANE_Y}
              r={RING_R}
              fill="var(--color-paper)"
              stroke="var(--color-copper)"
              strokeWidth={1.25}
            >
              <title>{`Freshness check · ${fmtAbsolute(c.checked_at)} · ${c.outcome}`}</title>
            </circle>
          );
        })}

        {sortedSnaps.map((s, i) => {
          const cx = snapXs[i];
          const showLabel = labelMask[i];
          return (
            <g key={`s-${s.id}`}>
              <circle cx={cx} cy={LANE_Y} r={DOT_R} fill="var(--color-copper)">
                <title>{`${fmtAbsolute(s.scraped_at)} · ${fmtCzk(s.price_czk)}`}</title>
              </circle>
              {showLabel && (
                <>
                  <line
                    x1={cx}
                    x2={cx}
                    y1={LANE_Y + DOT_R + 1}
                    y2={LANE_Y + 14}
                    stroke="var(--color-rule-strong)"
                    strokeWidth={1}
                  />
                  <text
                    x={cx}
                    y={LANE_Y + 25}
                    textAnchor="middle"
                    fontSize="10"
                    fill="var(--color-ink-2)"
                    style={{
                      fontFamily: 'var(--font-mono)',
                      fontVariantNumeric: 'tabular-nums',
                    }}
                  >
                    {compactCzk(s.price_czk)}
                  </text>
                </>
              )}
            </g>
          );
        })}

        <Anchor
          x={coverageStartX}
          label="First seen"
          sub={shortDate(firstSeenAt)}
          align="start"
        />
        <Anchor
          x={x(rightAnchorT)}
          label={isActive ? 'Today' : 'Last seen'}
          sub={isActive ? '' : shortDate(lastSeenAt)}
          align="end"
        />
      </svg>
    </div>
  );
}

function Anchor({
  x,
  label,
  sub,
  align,
}: {
  x: number;
  label: string;
  sub: string;
  align: 'start' | 'end';
}) {
  const anchor = align === 'start' ? 'start' : 'end';
  return (
    <g>
      <line
        x1={x}
        x2={x}
        y1={LANE_Y - 12}
        y2={LANE_Y + 12}
        stroke="var(--color-ink-3)"
        strokeWidth={1}
      />
      <text
        x={x}
        y={HEIGHT - 12}
        textAnchor={anchor}
        fontSize="9"
        fill="var(--color-ink-3)"
        style={{
          fontFamily: 'var(--font-sans)',
          letterSpacing: '0.14em',
          textTransform: 'uppercase',
        }}
      >
        {label}
      </text>
      {sub && (
        <text
          x={x}
          y={HEIGHT - 1}
          textAnchor={anchor}
          fontSize="10"
          fill="var(--color-ink-3)"
          style={{
            fontFamily: 'var(--font-mono)',
            fontVariantNumeric: 'tabular-nums',
          }}
        >
          {sub}
        </text>
      )}
    </g>
  );
}

function pickLabelMask(positions: number[], minPx: number): boolean[] {
  const n = positions.length;
  const mask = new Array<boolean>(n).fill(false);
  if (n === 0) return mask;
  if (n === 1) {
    mask[0] = true;
    return mask;
  }
  mask[0] = true;
  mask[n - 1] = true;
  let lastKept = positions[0];
  for (let i = 1; i < n - 1; i++) {
    if (
      positions[i] - lastKept >= minPx &&
      positions[n - 1] - positions[i] >= minPx
    ) {
      mask[i] = true;
      lastKept = positions[i];
    }
  }
  return mask;
}

function computeTicks(
  startT: number,
  endT: number,
): Array<{ ms: number; major: boolean }> {
  const days = (endT - startT) / DAY_MS;
  let smallStep: number;
  let bigEvery: number;
  if (days < 14) {
    smallStep = DAY_MS;
    bigEvery = 7;
  } else if (days < 90) {
    smallStep = 7 * DAY_MS;
    bigEvery = 4;
  } else if (days < 730) {
    smallStep = 30 * DAY_MS;
    bigEvery = 3;
  } else {
    smallStep = 90 * DAY_MS;
    bigEvery = 4;
  }
  const out: Array<{ ms: number; major: boolean }> = [];
  const first = Math.ceil(startT / smallStep) * smallStep;
  let i = 0;
  for (let ms = first; ms <= endT; ms += smallStep, i++) {
    out.push({ ms, major: i % bigEvery === 0 });
    if (out.length > 240) break;
  }
  return out;
}

function compactCzk(n: number | null | undefined): string {
  if (n == null) return '—';
  if (n >= 1_000_000) {
    const m = n / 1_000_000;
    return `${m.toFixed(m >= 10 ? 0 : 1).replace('.', ',')}M`;
  }
  if (n >= 1_000) {
    const k = n / 1_000;
    return `${k.toFixed(k >= 100 ? 0 : 1).replace('.', ',')}k`;
  }
  return String(n);
}

function shortDate(iso: string | null | undefined): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  return d.toLocaleDateString('cs-CZ', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  });
}
