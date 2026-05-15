/* Price-quartile turnover box plots backed by
 * browse_stats.price_quartile_velocity (migration 059). Four rows, one per
 * quartile (Q1=cheapest, Q4=priciest), showing the tom_days distribution of
 * each bucket on a shared horizontal scale.
 *
 * The active/inactive semantic of tom_days is set upstream by the user's
 * status filter in the Browse sidebar: status=active → "current age",
 * status=inactive → "time to delist", status=all → mixed. The component
 * itself stays unit-agnostic and renders whatever the RPC delivers.
 *
 * Styling intentionally mirrors DispositionBoxPlots (Tukey 1.5×IQR
 * whiskers, copper median line, hover-triggered numeric tooltip) so the
 * Stats tab reads as a cohesive surface rather than two parallel idioms.
 */

import { useMemo, useState } from 'react';
import type { PriceQuartileVelocityRow, TomBox } from '@/lib/queries';
import { fmtCount, fmtCzk } from '@/lib/format';

interface Props {
  rows: ReadonlyArray<PriceQuartileVelocityRow>;
}

const MIN_BOX_N = 5;

const NBSP = ' ';
const cz = new Intl.NumberFormat('cs-CZ');
const fmtDays = (n: number): string => `${cz.format(Math.round(n))}${NBSP}d`;
const fmtRange = (lo: number, hi: number): string =>
  `${fmtCzk(lo).replace(/ Kč$/, '')} – ${fmtCzk(hi)}`;

const PADDING_X = 16;
const LABEL_WIDTH = 152;
const ROW_HEIGHT = 40;
const BOX_HALF_HEIGHT = 9;
const WHISKER_CAP_HEIGHT = 6;
const AXIS_HEIGHT = 28;
const VIEW_WIDTH = 720;

interface Whiskers {
  low: number;
  high: number;
}

const whiskersFor = (b: TomBox): Whiskers => {
  const iqr = b.p75 - b.p25;
  const fenceLow = b.p25 - 1.5 * iqr;
  const fenceHigh = b.p75 + 1.5 * iqr;
  return {
    low: Math.max(b.min, fenceLow),
    high: Math.min(b.max, fenceHigh),
  };
};

const niceTickStep = (range: number): number => {
  if (range <= 0) return 1;
  const exp = Math.pow(10, Math.floor(Math.log10(range)));
  const norm = range / exp;
  let step;
  if (norm < 2) step = 0.2;
  else if (norm < 5) step = 0.5;
  else step = 1;
  return step * exp;
};

const niceBounds = (
  lo: number,
  hi: number,
): { lo: number; hi: number; step: number } => {
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || lo === hi) {
    const v = Number.isFinite(lo) ? lo : 0;
    return { lo: v - 1, hi: v + 1, step: 1 };
  }
  const step = niceTickStep(hi - lo);
  const niceLo = Math.floor(lo / step) * step;
  const niceHi = Math.ceil(hi / step) * step;
  return { lo: niceLo, hi: niceHi, step };
};

const BUCKET_LABELS: Record<1 | 2 | 3 | 4, string> = {
  1: 'Q1 · cheapest',
  2: 'Q2',
  3: 'Q3',
  4: 'Q4 · priciest',
};

export default function PriceQuartileVelocity({ rows }: Props) {
  const data = useMemo(() => rows.slice().sort((a, b) => a.bucket - b.bucket), [rows]);
  const renderable = data.filter(
    (r): r is PriceQuartileVelocityRow & { tom_box: TomBox } =>
      r.tom_box != null && r.tom_box.n >= MIN_BOX_N,
  );

  if (data.length === 0) {
    return (
      <p className="text-sm text-[var(--color-ink-3)] italic">
        Not enough priced listings in this filter to bucket by price quartile.
      </p>
    );
  }
  if (renderable.length === 0) {
    return (
      <p className="text-sm text-[var(--color-ink-3)] italic">
        Insufficient data for box plots (each bucket needs at least {MIN_BOX_N} listings with a tom_days value).
      </p>
    );
  }

  const allMins = renderable.map((r) => r.tom_box.min);
  const allMaxs = renderable.map((r) => r.tom_box.max);
  const dataLo = Math.min(...allMins);
  const dataHi = Math.max(...allMaxs);
  const { lo: scaleLo, hi: scaleHi, step } = niceBounds(dataLo, dataHi);
  const ticks: number[] = [];
  for (let v = scaleLo; v <= scaleHi + 1e-6; v += step) ticks.push(Math.round(v));

  const plotX0 = PADDING_X + LABEL_WIDTH;
  const plotX1 = VIEW_WIDTH - PADDING_X;
  const plotWidth = plotX1 - plotX0;
  const xOf = (v: number): number =>
    plotX0 + ((v - scaleLo) / (scaleHi - scaleLo || 1)) * plotWidth;

  const totalRows = data.length;
  const viewHeight = totalRows * ROW_HEIGHT + AXIS_HEIGHT;

  return (
    <div className="space-y-3">
      <div className="overflow-x-auto">
        <svg
          viewBox={`0 0 ${VIEW_WIDTH} ${viewHeight}`}
          className="w-full max-w-3xl"
          role="img"
          aria-label="Time on market by price quartile"
        >
          <line
            x1={plotX0}
            x2={plotX1}
            y1={viewHeight - AXIS_HEIGHT + 4}
            y2={viewHeight - AXIS_HEIGHT + 4}
            stroke="var(--color-rule-strong)"
            strokeWidth="1"
          />
          {ticks.map((t) => (
            <g key={t}>
              <line
                x1={xOf(t)}
                x2={xOf(t)}
                y1={viewHeight - AXIS_HEIGHT + 4}
                y2={viewHeight - AXIS_HEIGHT + 8}
                stroke="var(--color-rule-strong)"
                strokeWidth="1"
              />
              <text
                x={xOf(t)}
                y={viewHeight - AXIS_HEIGHT + 22}
                fontSize="10.5"
                fontFamily="var(--font-sans)"
                textAnchor="middle"
                fill="var(--color-ink-3)"
                style={{ fontVariantNumeric: 'tabular-nums' }}
              >
                {cz.format(t)}
              </text>
            </g>
          ))}
          <text
            x={plotX1}
            y={viewHeight - 2}
            fontSize="9.5"
            fontFamily="var(--font-sans)"
            textAnchor="end"
            fill="var(--color-ink-4)"
            style={{ letterSpacing: '0.04em' }}
          >
            tom_days
          </text>

          {data.map((row, i) => {
            const yMid = i * ROW_HEIGHT + ROW_HEIGHT / 2;
            const label = BUCKET_LABELS[row.bucket];
            return (
              <g key={row.bucket}>
                <text
                  x={PADDING_X + LABEL_WIDTH - 8}
                  y={yMid - 4}
                  fontSize="11.5"
                  fontFamily="var(--font-sans)"
                  textAnchor="end"
                  fill="var(--color-ink)"
                  style={{ fontWeight: 500 }}
                >
                  {label}
                </text>
                <text
                  x={PADDING_X + LABEL_WIDTH - 8}
                  y={yMid + 9}
                  fontSize="9.5"
                  fontFamily="var(--font-sans)"
                  textAnchor="end"
                  fill="var(--color-ink-3)"
                  style={{ fontVariantNumeric: 'tabular-nums' }}
                >
                  {fmtRange(row.price_min, row.price_max)}
                </text>
                <text
                  x={PADDING_X + LABEL_WIDTH - 8}
                  y={yMid + 21}
                  fontSize="9.5"
                  fontFamily="var(--font-sans)"
                  textAnchor="end"
                  fill="var(--color-ink-4)"
                  style={{ fontVariantNumeric: 'tabular-nums' }}
                >
                  n = {fmtCount(row.n)}
                </text>
                {row.tom_box && row.tom_box.n >= MIN_BOX_N ? (
                  <BoxRow box={row.tom_box} yMid={yMid} xOf={xOf} />
                ) : (
                  <InsufficientPlaceholder yMid={yMid} x0={plotX0} x1={plotX1} />
                )}
              </g>
            );
          })}
        </svg>
      </div>

      <NumericTable rows={data} />
    </div>
  );
}

function BoxRow({
  box,
  yMid,
  xOf,
}: {
  box: TomBox;
  yMid: number;
  xOf: (v: number) => number;
}) {
  const w = whiskersFor(box);
  const xMin = xOf(box.min);
  const xMax = xOf(box.max);
  const xWLow = xOf(w.low);
  const xWHigh = xOf(w.high);
  const xP25 = xOf(box.p25);
  const xP75 = xOf(box.p75);
  const xMed = xOf(box.median);
  const yTop = yMid - BOX_HALF_HEIGHT;
  const yBot = yMid + BOX_HALF_HEIGHT;

  const [hover, setHover] = useState(false);

  return (
    <g
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{ cursor: 'help' }}
    >
      <rect
        x={Math.min(xMin, xWLow) - 4}
        y={yTop - 4}
        width={Math.max(xMax, xWHigh) - Math.min(xMin, xWLow) + 8}
        height={(yBot - yTop) + 8}
        fill="transparent"
      />
      <line x1={xWLow} x2={xP25} y1={yMid} y2={yMid} stroke="var(--color-ink)" strokeWidth="1" />
      <line
        x1={xWLow}
        x2={xWLow}
        y1={yMid - WHISKER_CAP_HEIGHT}
        y2={yMid + WHISKER_CAP_HEIGHT}
        stroke="var(--color-ink)"
        strokeWidth="1"
      />
      <line x1={xP75} x2={xWHigh} y1={yMid} y2={yMid} stroke="var(--color-ink)" strokeWidth="1" />
      <line
        x1={xWHigh}
        x2={xWHigh}
        y1={yMid - WHISKER_CAP_HEIGHT}
        y2={yMid + WHISKER_CAP_HEIGHT}
        stroke="var(--color-ink)"
        strokeWidth="1"
      />
      <rect
        x={xP25}
        y={yTop}
        width={Math.max(1, xP75 - xP25)}
        height={yBot - yTop}
        fill="var(--color-paper-3)"
        stroke="var(--color-ink)"
        strokeWidth="1"
      />
      <line
        x1={xMed}
        x2={xMed}
        y1={yTop}
        y2={yBot}
        stroke="var(--color-copper)"
        strokeWidth="1.5"
      />
      <circle
        cx={xOf(box.mean)}
        cy={yMid}
        r="3"
        fill="var(--color-copper)"
        stroke="var(--color-paper-2)"
        strokeWidth="1.5"
      />

      {hover && (
        <foreignObject
          x={Math.min(xWHigh + 8, 480)}
          y={yMid - 60}
          width="200"
          height="120"
        >
          <BoxTooltip box={box} />
        </foreignObject>
      )}
    </g>
  );
}

function BoxTooltip({ box }: { box: TomBox }) {
  return (
    <div
      className="text-[0.7rem] bg-[var(--color-paper-3)] border border-[var(--color-rule-strong)] rounded-[var(--radius-sm)] p-2"
      style={{ boxShadow: '0 6px 18px rgba(0,0,0,0.08)' }}
    >
      <table className="w-full">
        <tbody>
          <Row label="max" v={box.max} />
          <Row label="p75" v={box.p75} />
          <Row label="mean" v={box.mean} />
          <Row label="median" v={box.median} bold />
          <Row label="p25" v={box.p25} />
          <Row label="min" v={box.min} />
        </tbody>
      </table>
    </div>
  );
}

function Row({ label, v, bold }: { label: string; v: number; bold?: boolean }) {
  return (
    <tr>
      <td className="text-[var(--color-ink-3)] pr-3">{label}</td>
      <td
        className={`text-right tabular-nums ${
          bold ? 'font-medium text-[var(--color-ink)]' : 'text-[var(--color-ink)]'
        }`}
      >
        {fmtDays(v)}
      </td>
    </tr>
  );
}

function InsufficientPlaceholder({
  yMid,
  x0,
  x1,
}: {
  yMid: number;
  x0: number;
  x1: number;
}) {
  return (
    <g>
      <line
        x1={x0}
        x2={x1}
        y1={yMid}
        y2={yMid}
        stroke="var(--color-rule)"
        strokeWidth="1"
        strokeDasharray="3 3"
      />
      <text
        x={(x0 + x1) / 2}
        y={yMid + 4}
        fontSize="10.5"
        fontFamily="var(--font-sans)"
        textAnchor="middle"
        fill="var(--color-ink-4)"
        fontStyle="italic"
      >
        insufficient data
      </text>
    </g>
  );
}

function NumericTable({ rows }: { rows: ReadonlyArray<PriceQuartileVelocityRow> }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs tabular-nums border border-[var(--color-rule)] rounded-[var(--radius-sm)] bg-[var(--color-paper-2)]">
        <thead>
          <tr className="text-left">
            <th className="px-3 py-2 font-medium text-[var(--color-ink-3)] tracking-wide uppercase text-[0.65rem]">
              Bucket
            </th>
            <th className="px-3 py-2 font-medium text-left text-[var(--color-ink-3)] tracking-wide uppercase text-[0.65rem]">
              Price range
            </th>
            <Th>n</Th>
            <Th>min</Th>
            <Th>p25</Th>
            <Th>median</Th>
            <Th>mean</Th>
            <Th>p75</Th>
            <Th>max</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.bucket} className="border-t border-[var(--color-rule-soft)]">
              <td className="px-3 py-1.5 text-[var(--color-ink)] font-medium">
                {BUCKET_LABELS[r.bucket]}
              </td>
              <td className="px-3 py-1.5 text-[var(--color-ink-2)]">
                {fmtRange(r.price_min, r.price_max)}
              </td>
              <Td>{fmtCount(r.n)}</Td>
              {r.tom_box ? (
                <>
                  <Td>{fmtDays(r.tom_box.min)}</Td>
                  <Td>{fmtDays(r.tom_box.p25)}</Td>
                  <Td bold>{fmtDays(r.tom_box.median)}</Td>
                  <Td>{fmtDays(r.tom_box.mean)}</Td>
                  <Td>{fmtDays(r.tom_box.p75)}</Td>
                  <Td>{fmtDays(r.tom_box.max)}</Td>
                </>
              ) : (
                <>
                  <Td>—</Td>
                  <Td>—</Td>
                  <Td>—</Td>
                  <Td>—</Td>
                  <Td>—</Td>
                  <Td>—</Td>
                </>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return (
    <th className="px-3 py-2 font-medium text-right text-[var(--color-ink-3)] tracking-wide uppercase text-[0.65rem]">
      {children}
    </th>
  );
}

function Td({ children, bold }: { children: React.ReactNode; bold?: boolean }) {
  return (
    <td
      className={`px-3 py-1.5 text-right ${
        bold ? 'font-medium text-[var(--color-ink)]' : 'text-[var(--color-ink-2)]'
      }`}
    >
      {children}
    </td>
  );
}
