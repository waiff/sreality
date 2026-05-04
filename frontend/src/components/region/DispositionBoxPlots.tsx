/* Per-disposition price-per-m² box plots backed by region_stats.dispositions[*].ppm2_box
 * (migration 021). One row per disposition that has a non-null ppm2_box; rows
 * with n < MIN_BOX_N render an "insufficient data" placeholder instead.
 *
 * Whisker convention: Tukey 1.5×IQR fences clipped to [min, max]. Individual
 * outliers are not drawn — the RPC doesn't return them and the whisker shape
 * + numeric tooltip already communicate spread.
 *
 * Colour: a single accent (--color-copper) for the median line; box border
 * + whiskers in --color-ink. Disposition rows are NOT colour-coded — they're
 * not categorical signals that benefit from differentiation.
 */

import { useMemo, useState } from 'react';
import type { Ppm2Box, RegionDispositionRow } from '@/lib/types';

interface Props {
  rows: RegionDispositionRow[];
}

const MIN_BOX_N = 5;

const NBSP = ' ';
const cz = new Intl.NumberFormat('cs-CZ');
const fmtPpm2 = (n: number): string => `${cz.format(Math.round(n))}${NBSP}Kč/m²`;

/* SVG geometry. The chart is drawn inside a fixed viewBox that the parent's
 * width scales via CSS; aspect ratio is row-count-driven. */
const PADDING_X = 16;
const LABEL_WIDTH = 112;
const ROW_HEIGHT = 36;
const BOX_HALF_HEIGHT = 9;
const WHISKER_CAP_HEIGHT = 6;
const AXIS_HEIGHT = 28;
const VIEW_WIDTH = 720;

interface Whiskers {
  low: number;
  high: number;
}

const whiskersFor = (b: Ppm2Box): Whiskers => {
  const iqr = b.p75 - b.p25;
  const fenceLow = b.p25 - 1.5 * iqr;
  const fenceHigh = b.p75 + 1.5 * iqr;
  return {
    low: Math.max(b.min, fenceLow),
    high: Math.min(b.max, fenceHigh),
  };
};

/* Round axis bounds to a friendly tick interval so the labels read cleanly. */
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

const niceBounds = (lo: number, hi: number): { lo: number; hi: number; step: number } => {
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || lo === hi) {
    const v = Number.isFinite(lo) ? lo : 0;
    return { lo: v - 1, hi: v + 1, step: 1 };
  }
  const step = niceTickStep(hi - lo);
  const niceLo = Math.floor(lo / step) * step;
  const niceHi = Math.ceil(hi / step) * step;
  return { lo: niceLo, hi: niceHi, step };
};

interface RenderRow {
  disposition: string;
  n: number;
  box: Ppm2Box | null;
}

export default function DispositionBoxPlots({ rows }: Props) {
  const data = useMemo<RenderRow[]>(
    () => rows.map((r) => ({ disposition: r.disposition, n: r.n, box: r.ppm2_box })),
    [rows],
  );

  const renderable = data.filter((r) => r.box != null && r.box.n >= MIN_BOX_N) as Array<
    RenderRow & { box: Ppm2Box }
  >;

  if (renderable.length === 0) {
    return (
      <p className="text-sm text-[var(--color-ink-3)] italic">
        Insufficient data for box plots (each disposition needs at least {MIN_BOX_N} listings with both price and area).
      </p>
    );
  }

  /* Shared horizontal scale across all rows so visual comparison is honest. */
  const allMins = renderable.map((r) => r.box.min);
  const allMaxs = renderable.map((r) => r.box.max);
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
          aria-label="Price per m² box plots by disposition"
        >
          {/* X-axis baseline + ticks. */}
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
            Kč/m²
          </text>

          {/* One row per disposition. */}
          {data.map((row, i) => {
            const yMid = i * ROW_HEIGHT + ROW_HEIGHT / 2;
            return (
              <g key={row.disposition}>
                <text
                  x={PADDING_X + LABEL_WIDTH - 8}
                  y={yMid + 4}
                  fontSize="11.5"
                  fontFamily="var(--font-sans)"
                  textAnchor="end"
                  fill="var(--color-ink)"
                  style={{ fontWeight: 500 }}
                >
                  {row.disposition}
                </text>
                <text
                  x={PADDING_X + LABEL_WIDTH - 8}
                  y={yMid + 17}
                  fontSize="9.5"
                  fontFamily="var(--font-sans)"
                  textAnchor="end"
                  fill="var(--color-ink-3)"
                  style={{ fontVariantNumeric: 'tabular-nums' }}
                >
                  n = {cz.format(row.n)}
                </text>
                {row.box && row.box.n >= MIN_BOX_N ? (
                  <BoxRow box={row.box} yMid={yMid} xOf={xOf} />
                ) : (
                  <InsufficientPlaceholder
                    yMid={yMid}
                    x0={plotX0}
                    x1={plotX1}
                  />
                )}
              </g>
            );
          })}
        </svg>
      </div>

      <NumericTable rows={renderable} />
    </div>
  );
}

function BoxRow({
  box,
  yMid,
  xOf,
}: {
  box: Ppm2Box;
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

  /* Whisker tooltip popover — positioned above the row. The skill says
   * borders-only as a rule, but tooltips overlapping the chart benefit
   * from a tiny offset shadow to lift them; matching the map-popover
   * carve-out in globals.css. */
  return (
    <g
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{ cursor: 'help' }}
    >
      {/* Hit-area rect for the whole row (transparent). */}
      <rect
        x={Math.min(xMin, xWLow) - 4}
        y={yTop - 4}
        width={Math.max(xMax, xWHigh) - Math.min(xMin, xWLow) + 8}
        height={(yBot - yTop) + 8}
        fill="transparent"
      />
      {/* Whisker line low → p25. */}
      <line
        x1={xWLow}
        x2={xP25}
        y1={yMid}
        y2={yMid}
        stroke="var(--color-ink)"
        strokeWidth="1"
      />
      <line
        x1={xWLow}
        x2={xWLow}
        y1={yMid - WHISKER_CAP_HEIGHT}
        y2={yMid + WHISKER_CAP_HEIGHT}
        stroke="var(--color-ink)"
        strokeWidth="1"
      />
      {/* Whisker line p75 → high. */}
      <line
        x1={xP75}
        x2={xWHigh}
        y1={yMid}
        y2={yMid}
        stroke="var(--color-ink)"
        strokeWidth="1"
      />
      <line
        x1={xWHigh}
        x2={xWHigh}
        y1={yMid - WHISKER_CAP_HEIGHT}
        y2={yMid + WHISKER_CAP_HEIGHT}
        stroke="var(--color-ink)"
        strokeWidth="1"
      />
      {/* IQR box. */}
      <rect
        x={xP25}
        y={yTop}
        width={Math.max(1, xP75 - xP25)}
        height={yBot - yTop}
        fill="var(--color-paper-3)"
        stroke="var(--color-ink)"
        strokeWidth="1"
      />
      {/* Median line — single accent. */}
      <line
        x1={xMed}
        x2={xMed}
        y1={yTop}
        y2={yBot}
        stroke="var(--color-copper)"
        strokeWidth="1.5"
      />

      {hover && (
        <foreignObject
          x={Math.min(xWHigh + 8, 480)}
          y={yMid - 60}
          width="240"
          height="120"
        >
          <BoxTooltip box={box} />
        </foreignObject>
      )}
    </g>
  );
}

function BoxTooltip({ box }: { box: Ppm2Box }) {
  return (
    <div
      className="text-[0.7rem] bg-[var(--color-paper-3)] border border-[var(--color-rule-strong)] rounded-[var(--radius-sm)] p-2"
      style={{ boxShadow: '0 6px 18px rgba(0,0,0,0.08)' }}
    >
      <table className="w-full">
        <tbody>
          <Row label="max" v={box.max} />
          <Row label="p75" v={box.p75} />
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
        className={`text-right tabular-nums ${bold ? 'font-medium text-[var(--color-ink)]' : 'text-[var(--color-ink)]'}`}
      >
        {fmtPpm2(v)}
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

/* -------------------------------------------------------------------------- */
/* Numeric table — precise readouts beneath the chart                         */
/* -------------------------------------------------------------------------- */

function NumericTable({ rows }: { rows: Array<RenderRow & { box: Ppm2Box }> }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs tabular-nums border border-[var(--color-rule)] rounded-[var(--radius-sm)] bg-[var(--color-paper-2)]">
        <thead>
          <tr className="text-left">
            <th className="px-3 py-2 font-medium text-[var(--color-ink-3)] tracking-wide uppercase text-[0.65rem]">Disposition</th>
            <Th>n</Th>
            <Th>min</Th>
            <Th>p25</Th>
            <Th>median</Th>
            <Th>p75</Th>
            <Th>max</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.disposition} className="border-t border-[var(--color-rule-soft)]">
              <td className="px-3 py-1.5 text-[var(--color-ink)] font-medium">{r.disposition}</td>
              <Td>{cz.format(r.box.n)}</Td>
              <Td>{cz.format(r.box.min)}</Td>
              <Td>{cz.format(r.box.p25)}</Td>
              <Td bold>{cz.format(r.box.median)}</Td>
              <Td>{cz.format(r.box.p75)}</Td>
              <Td>{cz.format(r.box.max)}</Td>
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
    <td className={`px-3 py-1.5 text-right ${bold ? 'font-medium text-[var(--color-ink)]' : 'text-[var(--color-ink-2)]'}`}>
      {children}
    </td>
  );
}
