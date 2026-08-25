import type { ReactNode } from 'react';
import type { BrowseStats } from '@/lib/queries';
import { fmtCount, fmtCzk } from '@/lib/format';
import {
  MIXED_BASIS_HINT,
  PPM2_UNIT,
  PRICE_PERIOD_UNIT,
  mixedBasisCause,
  pricePeriodOfCohort,
  type Ppm2Basis,
} from '@/lib/measure';
import DispositionBoxPlots from '@/components/region/DispositionBoxPlots';
import PriceBandVelocity from '@/components/PriceBandVelocity';

interface Props {
  stats: BrowseStats | null;
  isLoading: boolean;
  isEmpty: boolean;
  /* The cohort's per-m² basis, resolved server-side (BrowseStats.ppm2_basis).
   * Required, not optional: a Kč/m² percentile without one cannot be read.
   * 'mixed' / null withhold the figures rather than labelling them wrongly. */
  basis: Ppm2Basis | null;
  /* The cohort SPEC — the two filter fields that decide (a) whether an absolute
   * price is a monthly rent or a capital sum and (b) WHY a mixed basis is mixed.
   * Neither is answerable from `basis`: it is computed only over rows that HAVE
   * a measure, and its 'mixed' does not say which of the two mixes fired. */
  cohort: { categoryMain: ReadonlyArray<string>; categoryType: string | null };
  /* Per-disposition box-plot annotations (summarize-1), keyed by
   * disposition. Optional — the view renders fully without them. */
  annotations?: Record<string, string>;
  annotationsLoading?: boolean;
}

export default function BrowseStatsView({
  stats,
  isLoading,
  isEmpty,
  basis,
  cohort,
  annotations,
  annotationsLoading,
}: Props) {
  if (isLoading && !stats) return <Skeleton />;
  if (!stats) return null;
  if (isEmpty) return <Empty />;

  /* The absolute price's PERIOD, from the cohort's deal type alone. */
  const period = pricePeriodOfCohort(cohort.categoryType);

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <BigNumber label="New in last 7 days"  value={stats.new_7d}  />
        <BigNumber label="New in last 30 days" value={stats.new_30d} />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Card label="Total in filter set">
          <p className="text-3xl leading-none font-mono tabular-nums text-[var(--color-ink)]">
            {fmtCount(stats.total)}
          </p>
        </Card>
        {/* The absolute-price card was hardcoded "Kč / mo" — right for the
            default rent cohort, wrong for every sale one. Its period comes from
            the cohort's DEAL TYPE, deliberately NOT from the per-m² basis: that
            basis is computed only over rows that have a measure, so a rent
            cohort whose rows are all under the 1 000 Kč rent floor (or carry a
            NULL area) publishes a NULL basis while its prices are still monthly.
            A capital sale price and a capital land price pool perfectly well
            here — only a monthly rent stacked on a capital sum does not, and
            that is exactly what a null categoryType (the "Vše" pill) produces. */}
        <PercentileCard
          label="Price"
          unit={period == null || period === 'mixed' ? '' : PRICE_PERIOD_UNIT[period]}
          pct={period == null || period === 'mixed' ? null : stats.price}
          empty={
            period === 'mixed'
              ? MIXED_BASIS_HINT.deal
              : period == null
                ? '— neznámý typ nabídky'
                : undefined
          }
          fmt={(n) => fmtCzk(n).replace(/ Kč$/, '')}
        />
        {/* A mixed cohort's Kč/m² percentiles pool two denominators into one
            distribution — the median of ~91 535 and ~319 is not a price, it is
            an artefact of the mix. Withhold it, and say WHICH mix to clear:
            sale+rent needs one deal type, sale+land needs the pozemky out. */}
        <PercentileCard
          label="Price per m²"
          unit={basis == null || basis === 'mixed' ? '' : PPM2_UNIT[basis]}
          pct={basis == null || basis === 'mixed' ? null : stats.ppm2}
          empty={
            basis === 'mixed' ? MIXED_BASIS_HINT[mixedBasisCause(cohort)] : undefined
          }
          fmt={(n) => fmtCount(n)}
        />
      </div>

      <Card label="Disposition distribution">
        <DispositionBars
          rows={stats.dispositions}
          totalForShare={stats.total}
        />
      </Card>

      <Card label="Price per m² · by disposition">
        <p className="-mt-1 mb-3 text-[0.75rem] text-[var(--color-ink-3)]">
          Tukey 1.5×IQR whiskers clipped to min/max. Median in copper. Hover a box for the full numeric breakdown.
        </p>
        <DispositionBoxPlots
          basis={basis}
          mixedCause={mixedBasisCause(cohort)}
          rows={stats.dispositions.map((r) => ({
            disposition: r.disposition,
            n: r.n,
            median_price: null,
            median_ppm2: null,
            median_area: null,
            ppm2_box: r.ppm2_box,
          }))}
          annotations={annotations}
          annotationsLoading={annotationsLoading}
        />
      </Card>

      <Card label="Turnover by price band">
        <p className="-mt-1 mb-3 text-[0.75rem] text-[var(--color-ink-3)]">
          Cohort split into seven percentile bands by price — narrower
          at the tails (p0–p10, p90–p100) and around the median
          (p45–p55), wider through the body — so the chart can surface
          tail-vs-body differences that an equal-quartile split would
          mask. Box = tom_days distribution per band; copper bar is
          the median, copper dot is the mean. Active vs. delisted
          semantics follow the status filter above.
        </p>
        <PriceBandVelocity rows={stats.price_band_velocity} />
      </Card>
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function BigNumber({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4">
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        {label}
      </p>
      <p className="mt-2 font-display tabular-nums text-[2.4rem] leading-none tracking-tight text-[var(--color-ink)]">
        {fmtCount(value)}
      </p>
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function Card({ label, children }: { label: string; children: ReactNode }) {
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

function PercentileCard({
  label,
  unit,
  pct,
  fmt,
  empty,
}: {
  label: string;
  unit: string;
  pct: { p25: number; p50: number; p75: number } | null;
  fmt: (n: number) => string;
  /* Replaces the "no priced listings" line when the figures are withheld for a
   * reason the operator can act on, rather than simply being absent. */
  empty?: string;
}) {
  return (
    <Card label={`${label} percentiles`}>
      {pct == null ? (
        <p className="text-sm text-[var(--color-ink-4)]">{empty ?? '— no priced listings'}</p>
      ) : (
        <div className="grid grid-cols-3 gap-2">
          <PctCell tier="p25" value={fmt(pct.p25)} />
          <PctCell tier="median" value={fmt(pct.p50)} highlight />
          <PctCell tier="p75" value={fmt(pct.p75)} />
        </div>
      )}
      <p className="mt-2 text-[0.65rem] tracking-wide uppercase text-[var(--color-ink-4)]">
        {unit}
      </p>
    </Card>
  );
}

function PctCell({
  tier,
  value,
  highlight,
}: {
  tier: string;
  value: string;
  highlight?: boolean;
}) {
  return (
    <div>
      <p className="text-[0.62rem] tracking-[0.16em] uppercase text-[var(--color-ink-4)]">
        {tier}
      </p>
      <p
        className={[
          'mt-0.5 font-mono tabular-nums tracking-tight',
          highlight
            ? 'text-[var(--color-ink)] text-xl'
            : 'text-[var(--color-ink-2)] text-base',
        ].join(' ')}
      >
        {value}
      </p>
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function DispositionBars({
  rows,
  totalForShare,
}: {
  rows: ReadonlyArray<{ disposition: string; n: number }>;
  totalForShare: number;
}) {
  if (rows.length === 0) {
    return <p className="text-sm text-[var(--color-ink-4)]">No data.</p>;
  }
  const max = Math.max(...rows.map((r) => r.n));
  return (
    <ul className="space-y-2.5">
      {rows.map((r) => {
        const share = totalForShare > 0 ? (r.n / totalForShare) * 100 : 0;
        return (
          <li
            key={r.disposition}
            className="grid grid-cols-[5rem_1fr_5.5rem] sm:grid-cols-[6rem_1fr_7rem] gap-3 items-center"
          >
            <span className="font-mono tabular-nums text-sm text-[var(--color-ink-2)] truncate">
              {r.disposition}
            </span>
            <div
              className="h-1.5 bg-[var(--color-rule-soft)] rounded-full overflow-hidden"
              role="meter"
              aria-valuenow={r.n}
              aria-valuemax={max}
            >
              <div
                className="h-full bg-[var(--color-copper)] rounded-full transition-[width] duration-300"
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

function Skeleton() {
  return (
    <div className="space-y-5 animate-pulse">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <SkelCard h="6rem" />
        <SkelCard h="6rem" />
      </div>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <SkelCard h="6rem" />
        <SkelCard h="6rem" />
        <SkelCard h="6rem" />
      </div>
      <SkelCard h="14rem" />
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

function Empty() {
  return (
    <div className="rounded-[var(--radius-md)] border border-dashed border-[var(--color-rule)] p-12 text-center">
      <p className="text-sm text-[var(--color-ink-3)]">
        No listings match these filters — nothing to summarise.
      </p>
    </div>
  );
}
