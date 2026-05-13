import type { ReactNode } from 'react';
import type { BrowseStats } from '@/lib/queries';
import { fmtCount, fmtCzk } from '@/lib/format';
import DispositionBoxPlots from '@/components/region/DispositionBoxPlots';

interface Props {
  stats: BrowseStats | null;
  isLoading: boolean;
  isEmpty: boolean;
}

export default function BrowseStatsView({ stats, isLoading, isEmpty }: Props) {
  if (isLoading && !stats) return <Skeleton />;
  if (!stats) return null;
  if (isEmpty) return <Empty />;

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
        <PercentileCard
          label="Price"
          unit="Kč / mo"
          pct={stats.price}
          fmt={(n) => fmtCzk(n).replace(/ Kč$/, '')}
        />
        <PercentileCard
          label="Price per m²"
          unit="Kč / m²"
          pct={stats.ppm2}
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
          rows={stats.dispositions.map((r) => ({
            disposition: r.disposition,
            n: r.n,
            median_price: null,
            median_ppm2: null,
            median_area: null,
            ppm2_box: r.ppm2_box,
          }))}
        />
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
}: {
  label: string;
  unit: string;
  pct: { p25: number; p50: number; p75: number } | null;
  fmt: (n: number) => string;
}) {
  return (
    <Card label={`${label} percentiles`}>
      {pct == null ? (
        <p className="text-sm text-[var(--color-ink-4)]">— no priced listings</p>
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
