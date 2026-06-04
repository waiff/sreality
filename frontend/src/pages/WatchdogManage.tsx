import { Link } from 'react-router-dom';
import {
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query';

import {
  deleteWatchdogSubscription,
  listWatchdogSubscriptions,
  updateWatchdogSubscription,
} from '@/lib/api';
import { watchdogKeys } from '@/lib/queries';
import { fmtAbsolute, fmtCount, fmtRelative } from '@/lib/format';
import type { WatchdogFilterSpec, WatchdogSubscription } from '@/lib/types';

export default function WatchdogManage() {
  const qc = useQueryClient();
  const listQ = useQuery({
    queryKey: watchdogKeys.subscriptions,
    queryFn: () => listWatchdogSubscriptions(),
  });

  const toggleMut = useMutation({
    mutationFn: (vars: { id: string; is_active: boolean }) =>
      updateWatchdogSubscription(vars.id, { is_active: vars.is_active }),
    onSuccess: () => qc.invalidateQueries({ queryKey: watchdogKeys.all }),
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => deleteWatchdogSubscription(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: watchdogKeys.all }),
  });

  return (
    <div className="px-6 py-8 max-w-5xl mx-auto">
      <Header />

      <div className="mt-6">
        {listQ.isLoading ? (
          <div className="text-sm text-[var(--color-ink-3)]">Loading…</div>
        ) : listQ.error ? (
          <div className="text-sm text-[var(--color-brick)]">
            Failed to load: {listQ.error.message}
          </div>
        ) : !listQ.data || listQ.data.data.length === 0 ? (
          <EmptyState />
        ) : (
          <SubscriptionsTable
            rows={listQ.data.data}
            onToggle={(id, next) =>
              toggleMut.mutate({ id, is_active: next })
            }
            onDelete={(id) => {
              if (
                window.confirm(
                  'Delete this watchdog? Its notification history will be removed too.',
                )
              ) {
                deleteMut.mutate(id);
              }
            }}
          />
        )}
      </div>
    </div>
  );
}

function Header() {
  return (
    <header className="flex flex-wrap items-end justify-between gap-4">
      <div>
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          Watchdog
        </p>
        <h1
          className="mt-1.5 text-[2.1rem] leading-tight"
          style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
        >
          Manage watchdogs
        </h1>
        <p className="mt-2 text-sm text-[var(--color-ink-2)]">
          A watchdog is a saved filter. The backend matcher fires a
          notification when a freshly scraped listing matches the spec.
        </p>
      </div>
      <div className="flex items-center gap-3">
        <Link
          to="/watchdog"
          className="px-3 py-1.5 text-sm rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink-2)] hover:text-[var(--color-ink)] hover:border-[var(--color-rule-strong)] transition-colors"
        >
          ← Back to feed
        </Link>
        <Link
          to="/browse"
          className="px-3 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)] transition-colors"
          title="Watchdogs are created by saving a filter on the Browse page."
        >
          + New in Browse
        </Link>
      </div>
    </header>
  );
}

function SubscriptionsTable({
  rows,
  onToggle,
  onDelete,
}: {
  rows: WatchdogSubscription[];
  onToggle: (id: string, nextActive: boolean) => void;
  onDelete: (id: string) => void;
}) {
  return (
    <div className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-[var(--color-paper-2)] border-b border-[var(--color-rule)]">
            <tr>
              <Th align="left">Name</Th>
              <Th align="left">Filter summary</Th>
              <Th align="right">Fired</Th>
              <Th align="left">Created</Th>
              <Th align="left">Status</Th>
              <Th align="right"> </Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((s) => (
              <SubRow
                key={s.id}
                sub={s}
                onToggle={() => onToggle(s.id, !s.is_active)}
                onDelete={() => onDelete(s.id)}
              />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SubRow({
  sub,
  onToggle,
  onDelete,
}: {
  sub: WatchdogSubscription;
  onToggle: () => void;
  onDelete: () => void;
}) {
  const summary = summariseFilter(sub.filter_spec);
  return (
    <tr className="border-b border-[var(--color-rule-soft)] last:border-b-0 hover:bg-[var(--color-paper)] transition-colors">
      <td className="px-4 py-2.5 align-middle">
        <Link
          to={`/watchdog/${encodeURIComponent(sub.id)}/edit`}
          className="text-[var(--color-ink)] hover:text-[var(--color-copper)] hover:underline underline-offset-2"
        >
          {sub.name}
        </Link>
      </td>
      <td className="px-4 py-2.5 align-middle text-[var(--color-ink-2)] max-w-[420px]">
        <span className="block truncate" title={summary}>
          {summary}
        </span>
      </td>
      <td className="px-4 py-2.5 align-middle text-right tabular-nums text-[var(--color-ink-2)]">
        {fmtCount(sub.dispatch_count)}
      </td>
      <td
        className="px-4 py-2.5 align-middle text-[var(--color-ink-3)] tabular-nums"
        title={fmtAbsolute(sub.created_at)}
      >
        {fmtRelative(sub.created_at)}
      </td>
      <td className="px-4 py-2.5 align-middle">
        <button
          type="button"
          onClick={onToggle}
          aria-pressed={sub.is_active}
          className={[
            'inline-flex items-center gap-1.5 text-[0.7rem] tracking-wide uppercase rounded-[var(--radius-xs)] px-2 py-0.5 border transition-colors',
            sub.is_active
              ? 'border-[var(--color-sage)]/40 text-[var(--color-sage)] hover:bg-[var(--color-sage-soft)]'
              : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]',
          ].join(' ')}
          title={sub.is_active ? 'Pause this watchdog' : 'Resume this watchdog'}
        >
          <span
            className={[
              'w-1.5 h-1.5 rounded-full',
              sub.is_active ? 'bg-[var(--color-sage)]' : 'bg-[var(--color-ink-4)]',
            ].join(' ')}
            aria-hidden
          />
          {sub.is_active ? 'active' : 'paused'}
        </button>
      </td>
      <td className="px-4 py-2.5 align-middle text-right">
        <Link
          to={`/watchdog/${encodeURIComponent(sub.id)}/edit`}
          className="mr-3 text-[0.75rem] text-[var(--color-copper)] hover:underline underline-offset-2"
        >
          edit
        </Link>
        <button
          type="button"
          onClick={onDelete}
          className="text-[0.75rem] text-[var(--color-brick)] hover:underline underline-offset-2"
        >
          delete
        </button>
      </td>
    </tr>
  );
}

function Th({
  align,
  children,
}: {
  align: 'left' | 'right';
  children: React.ReactNode;
}) {
  return (
    <th
      scope="col"
      className={[
        'px-4 py-2.5 text-[0.7rem] tracking-[0.14em] uppercase font-medium text-[var(--color-ink-3)]',
        align === 'right' ? 'text-right' : 'text-left',
      ].join(' ')}
    >
      {children}
    </th>
  );
}

function EmptyState() {
  return (
    <div className="px-6 py-16 text-center border border-dashed border-[var(--color-rule)] rounded-[var(--radius-md)]">
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        No watchdogs yet
      </p>
      <p className="mt-2 text-sm text-[var(--color-ink-2)]">
        Watchdogs are saved Browse filters. Set up your filters on the
        Browse page and click “Create watchdog” to start receiving
        notifications whenever a new listing matches.
      </p>
      <Link
        to="/browse"
        className="mt-4 inline-block text-sm text-[var(--color-copper)] hover:underline underline-offset-2"
      >
        Go to Browse →
      </Link>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Filter summary renderer — kept here so the table row stays tight and the   */
/* edit form owns its own labelling.                                          */
/* -------------------------------------------------------------------------- */

const FURNISHED_TEXT: Record<string, string> = {
  ano: 'furnished',
  ne: 'unfurnished',
  castecne: 'part-furnished',
  __unknown__: 'furnishing unknown',
};

const OWNERSHIP_TEXT: Record<string, string> = {
  osobni: 'personal',
  druzstevni: 'cooperative',
  statni: 'state/municipal',
  __unknown__: 'ownership unknown',
};

function summariseFilter(spec: WatchdogFilterSpec): string {
  const bits: string[] = [];
  const cat = spec.category_main ?? 'any category';
  const deal = spec.category_type ?? 'any deal';
  bits.push(`${cat} / ${deal}`);
  if (spec.dispositions && spec.dispositions.length) {
    bits.push(spec.dispositions.join(', '));
  }
  if (spec.districts && spec.districts.length) {
    const head = spec.districts
      .slice(0, 3)
      .map((d) => {
        const base = d.context ? `${d.name} · ${d.context}` : d.name;
        return d.excluded ? `−${base}` : base;
      })
      .join(', ');
    bits.push(
      spec.districts.length > 3
        ? `${head} +${spec.districts.length - 3}`
        : head,
    );
  }
  if (
    spec.lat != null &&
    spec.lng != null &&
    spec.radius_m != null
  ) {
    bits.push(
      `within ${spec.radius_m} m of (${spec.lat.toFixed(4)}, ${spec.lng.toFixed(4)})`,
    );
  }
  if (spec.min_price_czk != null || spec.max_price_czk != null) {
    const lo = spec.min_price_czk != null ? `${spec.min_price_czk.toLocaleString()}` : '0';
    const hi = spec.max_price_czk != null ? `${spec.max_price_czk.toLocaleString()}` : '∞';
    bits.push(`${lo}–${hi} Kč`);
  }
  if (spec.min_area_m2 != null || spec.max_area_m2 != null) {
    const lo = spec.min_area_m2 != null ? `${spec.min_area_m2}` : '0';
    const hi = spec.max_area_m2 != null ? `${spec.max_area_m2}` : '∞';
    bits.push(`${lo}–${hi} m²`);
  }
  if (spec.has_balcony === true) bits.push('balcony');
  if (spec.has_lift === true) bits.push('lift');
  if (spec.has_parking === true) bits.push('parking');
  if (spec.terrace === true) bits.push('terrace');
  if (spec.cellar === true) bits.push('cellar');
  if (spec.garage === true) bits.push('garage');
  if (spec.furnished?.length) {
    bits.push(spec.furnished.map((v) => FURNISHED_TEXT[v] ?? v).join(', '));
  }
  if (spec.ownership?.length) {
    bits.push(spec.ownership.map((v) => OWNERSHIP_TEXT[v] ?? v).join(', '));
  }
  if (spec.min_parking_lots != null) bits.push(`≥${spec.min_parking_lots} parking`);
  if (spec.building_condition_level_min != null) bits.push(`bld≥${spec.building_condition_level_min}`);
  if (spec.apartment_condition_level_min != null) bits.push(`apt≥${spec.apartment_condition_level_min}`);
  return bits.join(' · ');
}
