import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import {
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query';

import {
  createWatchdogSubscription,
  getWatchdogSubscription,
  updateWatchdogSubscription,
} from '@/lib/api';
import { watchdogKeys } from '@/lib/queries';
import {
  DEFAULT_WATCHDOG_FILTER_SPEC,
  type Disposition,
  type Furnished,
  type Ownership,
  type WatchdogFilterSpec,
} from '@/lib/types';

const ALL_DISPOSITIONS: ReadonlyArray<Disposition> = [
  '1+kk', '1+1', '2+kk', '2+1',
  '3+kk', '3+1', '4+kk', '4+1',
  '5+kk', '5+1',
];

const FURNISHED_LABELS: Record<Furnished, string> = {
  ano: 'Furnished',
  ne: 'Unfurnished',
  castecne: 'Part-furnished',
};

const OWNERSHIP_LABELS: Record<Ownership, string> = {
  osobni: 'Osobní',
  druzstevni: 'Družstevní',
  statni: 'Státní/obecní',
};

type TriState = 'any' | 'yes' | 'no';

const boolToTri = (v: boolean | null): TriState =>
  v == null ? 'any' : v ? 'yes' : 'no';
const triToBool = (v: TriState): boolean | null =>
  v === 'any' ? null : v === 'yes';

export default function WatchdogEdit() {
  const { id } = useParams<{ id?: string }>();
  const isEdit = Boolean(id);
  const navigate = useNavigate();
  const qc = useQueryClient();

  const existingQ = useQuery({
    queryKey: id ? watchdogKeys.subscription(id) : ['watchdog', 'new'],
    queryFn: () => (id ? getWatchdogSubscription(id) : Promise.resolve(null)),
    enabled: isEdit,
  });

  const [name, setName] = useState('');
  const [spec, setSpec] = useState<WatchdogFilterSpec>(
    DEFAULT_WATCHDOG_FILTER_SPEC,
  );
  const [isActive, setIsActive] = useState(true);
  const [hydrated, setHydrated] = useState(!isEdit);

  useEffect(() => {
    if (isEdit && existingQ.data && !hydrated) {
      setName(existingQ.data.name);
      setSpec({ ...DEFAULT_WATCHDOG_FILTER_SPEC, ...existingQ.data.filter_spec });
      setIsActive(existingQ.data.is_active);
      setHydrated(true);
    }
  }, [isEdit, existingQ.data, hydrated]);

  const saveMut = useMutation({
    mutationFn: async () => {
      if (isEdit && id) {
        return updateWatchdogSubscription(id, {
          name,
          filter_spec: spec,
          is_active: isActive,
        });
      }
      return createWatchdogSubscription({
        name,
        filter_spec: spec,
        is_active: isActive,
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: watchdogKeys.all });
      navigate('/watchdog/manage');
    },
  });

  const set = <K extends keyof WatchdogFilterSpec>(
    key: K,
    value: WatchdogFilterSpec[K],
  ) => setSpec((prev) => ({ ...prev, [key]: value }));

  const canSave = name.trim().length > 0 && !saveMut.isPending;
  const submitError = saveMut.error?.message ?? null;

  return (
    <div className="px-6 py-8 max-w-3xl mx-auto">
      <Header isEdit={isEdit} />

      <form
        className="mt-6 space-y-8"
        onSubmit={(e) => {
          e.preventDefault();
          if (canSave) saveMut.mutate();
        }}
      >
        <Section title="Identity">
          <Row label="Name">
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. 2+kk Praha 2 under 25 000"
              className="w-full px-3 py-2 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)]"
              required
            />
          </Row>
          <Row label="Status">
            <Toggle
              on={isActive}
              onChange={setIsActive}
              labelOn="Active"
              labelOff="Paused"
            />
          </Row>
        </Section>

        <Section title="Category">
          <Row label="Category main">
            <SegmentedSelect<string | null>
              value={spec.category_main}
              options={[
                { value: 'byt', label: 'Apartments' },
                { value: 'dum', label: 'Houses' },
                { value: 'komercni', label: 'Commercial' },
                { value: null, label: 'Any' },
              ]}
              onChange={(v) => set('category_main', v)}
            />
          </Row>
          <Row label="Deal">
            <SegmentedSelect<string | null>
              value={spec.category_type}
              options={[
                { value: 'pronajem', label: 'Rent' },
                { value: 'prodej', label: 'Sale' },
                { value: null, label: 'Any' },
              ]}
              onChange={(v) => set('category_type', v)}
            />
          </Row>
          <Row label="Dispositions">
            <DispositionGrid
              value={spec.dispositions ?? []}
              onChange={(v) => set('dispositions', v.length ? v : null)}
            />
          </Row>
        </Section>

        <Section title="Where">
          <Row label="District name(s)">
            <CsvInput
              value={spec.districts ?? []}
              placeholder="e.g. Praha 2, Praha 5"
              onChange={(v) => set('districts', v.length ? v : null)}
            />
            <p className="mt-1 text-[0.7rem] text-[var(--color-ink-4)]">
              Comma-separated. Matched verbatim against the listing's
              <code className="mx-1 text-[var(--color-ink-3)]">district</code> column.
            </p>
          </Row>
          <Row label="Spatial center">
            <div className="grid grid-cols-3 gap-2">
              <NumberInput
                value={spec.lat}
                placeholder="lat"
                step="0.000001"
                onChange={(v) => set('lat', v)}
              />
              <NumberInput
                value={spec.lng}
                placeholder="lng"
                step="0.000001"
                onChange={(v) => set('lng', v)}
              />
              <NumberInput
                value={spec.radius_m}
                placeholder="radius m"
                step="50"
                onChange={(v) => set('radius_m', v == null ? null : Math.trunc(v))}
              />
            </div>
            <p className="mt-1 text-[0.7rem] text-[var(--color-ink-4)]">
              Optional. All three must be set together. Restricts the
              watchdog to listings within the circle.
            </p>
          </Row>
        </Section>

        <Section title="Price &amp; size">
          <Row label="Price Kč">
            <RangePair
              min={spec.min_price_czk}
              max={spec.max_price_czk}
              minPlaceholder="min"
              maxPlaceholder="max"
              onChange={(lo, hi) => {
                set('min_price_czk', lo == null ? null : Math.trunc(lo));
                set('max_price_czk', hi == null ? null : Math.trunc(hi));
              }}
            />
          </Row>
          <Row label="Area m²">
            <RangePair
              min={spec.min_area_m2}
              max={spec.max_area_m2}
              minPlaceholder="min"
              maxPlaceholder="max"
              onChange={(lo, hi) => {
                set('min_area_m2', lo);
                set('max_area_m2', hi);
              }}
            />
          </Row>
          <Row label="Usable area m²">
            <RangePair
              min={spec.min_usable_area}
              max={spec.max_usable_area}
              minPlaceholder="min"
              maxPlaceholder="max"
              onChange={(lo, hi) => {
                set('min_usable_area', lo);
                set('max_usable_area', hi);
              }}
            />
          </Row>
          <Row label="Estate area m²">
            <RangePair
              min={spec.min_estate_area}
              max={spec.max_estate_area}
              minPlaceholder="min"
              maxPlaceholder="max"
              onChange={(lo, hi) => {
                set('min_estate_area', lo);
                set('max_estate_area', hi);
              }}
            />
          </Row>
          <Row label="Min parking lots">
            <NumberInput
              value={spec.min_parking_lots}
              placeholder="0"
              step="1"
              onChange={(v) =>
                set('min_parking_lots', v == null ? null : Math.trunc(v))
              }
            />
          </Row>
        </Section>

        <Section title="Amenities">
          <TriRow
            label="Balcony"
            value={boolToTri(spec.has_balcony)}
            onChange={(v) => set('has_balcony', triToBool(v))}
          />
          <TriRow
            label="Lift"
            value={boolToTri(spec.has_lift)}
            onChange={(v) => set('has_lift', triToBool(v))}
          />
          <TriRow
            label="Parking"
            value={boolToTri(spec.has_parking)}
            onChange={(v) => set('has_parking', triToBool(v))}
          />
          <TriRow
            label="Terrace"
            value={boolToTri(spec.terrace)}
            onChange={(v) => set('terrace', triToBool(v))}
          />
          <TriRow
            label="Cellar"
            value={boolToTri(spec.cellar)}
            onChange={(v) => set('cellar', triToBool(v))}
          />
          <TriRow
            label="Garage"
            value={boolToTri(spec.garage)}
            onChange={(v) => set('garage', triToBool(v))}
          />
        </Section>

        <Section title="Other">
          <Row label="Furnished">
            <EnumSelect<Furnished>
              value={spec.furnished}
              labels={FURNISHED_LABELS}
              onChange={(v) => set('furnished', v)}
            />
          </Row>
          <Row label="Ownership">
            <EnumSelect<Ownership>
              value={spec.ownership}
              labels={OWNERSHIP_LABELS}
              onChange={(v) => set('ownership', v)}
            />
          </Row>
        </Section>

        {submitError ? (
          <p className="text-sm text-[var(--color-brick)]">{submitError}</p>
        ) : null}

        <div className="flex items-center justify-between gap-3 border-t border-[var(--color-rule)] pt-5">
          <Link
            to="/watchdog/manage"
            className="text-sm text-[var(--color-ink-3)] hover:text-[var(--color-ink)]"
          >
            ← Cancel
          </Link>
          <button
            type="submit"
            disabled={!canSave}
            className="px-4 py-2 text-sm rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)] transition-colors disabled:opacity-50"
          >
            {saveMut.isPending
              ? 'Saving…'
              : isEdit
                ? 'Save changes'
                : 'Create watchdog'}
          </button>
        </div>
      </form>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Header                                                                     */
/* -------------------------------------------------------------------------- */

function Header({ isEdit }: { isEdit: boolean }) {
  return (
    <header>
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        Watchdog
      </p>
      <h1
        className="mt-1.5 text-[2.1rem] leading-tight"
        style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
      >
        {isEdit ? 'Edit watchdog' : 'New watchdog'}
      </h1>
      <p className="mt-2 text-sm text-[var(--color-ink-2)]">
        Set the filter once; the matcher fires a notification whenever
        a newly scraped listing matches.
      </p>
    </header>
  );
}

/* -------------------------------------------------------------------------- */
/* Form scaffolding                                                           */
/* -------------------------------------------------------------------------- */

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <fieldset className="m-0 p-0 border-0 border-t border-[var(--color-rule-strong)] pt-5 first:border-t-0 first:pt-0">
      <legend
        className="block w-full mb-3 text-[0.65rem] tracking-[0.22em] uppercase text-[var(--color-ink-2)] font-medium"
        style={{ fontFamily: 'var(--font-display)' }}
      >
        {title}
      </legend>
      <div className="space-y-4">{children}</div>
    </fieldset>
  );
}

function Row({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="grid grid-cols-[140px_1fr] items-start gap-3">
      <span className="pt-1.5 text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
        {label}
      </span>
      <div>{children}</div>
    </div>
  );
}

function Toggle({
  on,
  onChange,
  labelOn,
  labelOff,
}: {
  on: boolean;
  onChange: (v: boolean) => void;
  labelOn: string;
  labelOff: string;
}) {
  return (
    <button
      type="button"
      onClick={() => onChange(!on)}
      aria-pressed={on}
      className={[
        'inline-flex items-center gap-2 px-3 py-1.5 rounded-[var(--radius-sm)] border text-sm transition-colors',
        on
          ? 'border-[var(--color-sage)]/40 text-[var(--color-sage)] bg-[var(--color-sage-soft)]/40'
          : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]',
      ].join(' ')}
    >
      <span
        className={[
          'w-1.5 h-1.5 rounded-full',
          on ? 'bg-[var(--color-sage)]' : 'bg-[var(--color-ink-4)]',
        ].join(' ')}
        aria-hidden
      />
      {on ? labelOn : labelOff}
    </button>
  );
}

/* -------------------------------------------------------------------------- */
/* Specialised inputs                                                         */
/* -------------------------------------------------------------------------- */

function SegmentedSelect<T extends string | null>({
  value,
  options,
  onChange,
}: {
  value: T;
  options: ReadonlyArray<{ value: T; label: string }>;
  onChange: (v: T) => void;
}) {
  return (
    <div className="inline-flex items-center gap-0.5 p-0.5 rounded-[var(--radius-sm)] bg-[var(--color-paper-2)] border border-[var(--color-rule)]">
      {options.map((opt) => {
        const on = value === opt.value;
        return (
          <button
            key={String(opt.value)}
            type="button"
            onClick={() => onChange(opt.value)}
            aria-pressed={on}
            className={[
              'px-2.5 py-0.5 text-[0.75rem] rounded-[var(--radius-xs)] transition-colors',
              on
                ? 'bg-[var(--color-copper)] text-white'
                : 'text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]',
            ].join(' ')}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}

function DispositionGrid({
  value,
  onChange,
}: {
  value: string[];
  onChange: (v: string[]) => void;
}) {
  const toggle = (d: string) => {
    if (value.includes(d)) onChange(value.filter((x) => x !== d));
    else onChange([...value, d]);
  };
  return (
    <div className="grid grid-cols-5 gap-1.5">
      {ALL_DISPOSITIONS.map((d) => {
        const on = value.includes(d);
        return (
          <button
            key={d}
            type="button"
            onClick={() => toggle(d)}
            aria-pressed={on}
            className={[
              'px-2 py-1.5 text-xs rounded-[var(--radius-sm)] border transition-colors',
              on
                ? 'bg-[var(--color-copper)] text-white border-[var(--color-copper)]'
                : 'bg-[var(--color-paper-2)] text-[var(--color-ink-3)] border-[var(--color-rule)] hover:text-[var(--color-ink-2)]',
            ].join(' ')}
          >
            {d}
          </button>
        );
      })}
    </div>
  );
}

function CsvInput({
  value,
  placeholder,
  onChange,
}: {
  value: string[];
  placeholder: string;
  onChange: (v: string[]) => void;
}) {
  return (
    <input
      type="text"
      value={value.join(', ')}
      placeholder={placeholder}
      onChange={(e) => {
        const parts = e.target.value
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean);
        onChange(parts);
      }}
      className="w-full px-3 py-2 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)]"
    />
  );
}

function NumberInput({
  value,
  placeholder,
  step,
  onChange,
}: {
  value: number | null;
  placeholder: string;
  step: string;
  onChange: (v: number | null) => void;
}) {
  return (
    <input
      type="number"
      inputMode="decimal"
      step={step}
      value={value ?? ''}
      placeholder={placeholder}
      onChange={(e) => {
        const raw = e.target.value.trim();
        if (raw === '') {
          onChange(null);
          return;
        }
        const n = Number(raw);
        onChange(Number.isFinite(n) ? n : null);
      }}
      className="w-full px-3 py-2 text-sm font-mono tabular-nums rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)]"
    />
  );
}

function RangePair({
  min,
  max,
  minPlaceholder,
  maxPlaceholder,
  onChange,
}: {
  min: number | null;
  max: number | null;
  minPlaceholder: string;
  maxPlaceholder: string;
  onChange: (lo: number | null, hi: number | null) => void;
}) {
  return (
    <div className="grid grid-cols-2 gap-2">
      <NumberInput
        value={min}
        placeholder={minPlaceholder}
        step="any"
        onChange={(v) => onChange(v, max)}
      />
      <NumberInput
        value={max}
        placeholder={maxPlaceholder}
        step="any"
        onChange={(v) => onChange(min, v)}
      />
    </div>
  );
}

function TriRow({
  label,
  value,
  onChange,
}: {
  label: string;
  value: TriState;
  onChange: (v: TriState) => void;
}) {
  return (
    <div className="grid grid-cols-[140px_1fr] items-center gap-3">
      <span className="text-[0.7rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
        {label}
      </span>
      <div className="inline-flex w-fit items-center gap-0.5 p-0.5 rounded-[var(--radius-sm)] bg-[var(--color-paper-2)] border border-[var(--color-rule)]">
        {(['any', 'yes', 'no'] as const).map((opt) => (
          <button
            key={opt}
            type="button"
            onClick={() => onChange(opt)}
            aria-pressed={value === opt}
            className={[
              'px-2.5 py-0.5 text-[0.7rem] rounded-[var(--radius-xs)] transition-colors',
              value === opt
                ? 'bg-[var(--color-copper)] text-white'
                : 'text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)]',
            ].join(' ')}
          >
            {opt}
          </button>
        ))}
      </div>
    </div>
  );
}

function EnumSelect<T extends string>({
  value,
  labels,
  onChange,
}: {
  value: T | null;
  labels: Record<T, string>;
  onChange: (v: T | null) => void;
}) {
  const opts = useMemo(
    () =>
      (Object.keys(labels) as T[]).map((v) => ({
        value: v,
        label: labels[v],
      })),
    [labels],
  );
  return (
    <select
      value={value ?? ''}
      onChange={(e) => onChange((e.target.value as T) || null)}
      className="px-2.5 py-2 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)]"
    >
      <option value="">Any</option>
      {opts.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}
