import { useMemo } from 'react';
import type {
  Confidence,
  Disposition,
  PreviewListing,
  TargetSpecIn,
} from '@/lib/types';

/* -------------------------------------------------------------------------- */
/* Form state — captured at the page level, mutated through this component.   */
/* The shape is a superset of TargetIn + a few display-only target attributes */
/* + the filter parameters. Step 6 packs this into CreateEstimationIn.        */
/* -------------------------------------------------------------------------- */

export interface EstimateFormState {
  // Spec — TargetIn fields.
  lat: number | null;
  lng: number | null;
  area_m2: number | null;
  disposition: Disposition | null;
  floor: number | null;
  exclude_ids: number[];

  // Target attributes — pre-filled from scrape, edited for context. On submit
  // the *_match/has_* values are folded into ComparableFilters.
  total_floors: number | null;
  building_type: string | null;
  condition: string | null;
  energy_rating: string | null;
  has_balcony: TriValue;
  has_lift: TriValue;
  has_parking: TriValue;

  // Yield.
  purchase_price_czk: number | null;

  // Filter parameters (Advanced).
  radius_m: number;
  area_band_pct: number;
  disposition_match: 'exact' | 'loose' | 'any';
  max_age_days: number;
  active_only: boolean;
}

export type TriValue = 'any' | 'yes' | 'no';

export const FORM_DEFAULTS = {
  radius_m: 1000,
  area_band_pct: 0.20,
  disposition_match: 'exact' as const,
  max_age_days: 7,
  active_only: true,
};

export const DISPOSITIONS: ReadonlyArray<Disposition> = [
  '1+kk', '1+1',
  '2+kk', '2+1',
  '3+kk', '3+1',
  '4+kk', '4+1',
  '5+kk', '5+1',
];

export function buildInitialFormState(
  spec: TargetSpecIn,
  listing: PreviewListing,
): EstimateFormState {
  return {
    lat: spec.lat,
    lng: spec.lng,
    area_m2: spec.area_m2,
    disposition: spec.disposition,
    floor: spec.floor,
    exclude_ids: spec.exclude_ids,
    total_floors: listing.total_floors,
    building_type: listing.building_type,
    condition: listing.condition,
    energy_rating: listing.energy_rating,
    has_balcony: triFromBool(listing.has_balcony),
    has_lift: triFromBool(listing.has_lift),
    has_parking: triFromBool(listing.has_parking),
    purchase_price_czk: null,
    ...FORM_DEFAULTS,
  };
}

function triFromBool(v: boolean | null): TriValue {
  if (v === true) return 'yes';
  if (v === false) return 'no';
  return 'any';
}

export function isFormValid(s: EstimateFormState): boolean {
  return (
    s.lat != null && Number.isFinite(s.lat) &&
    s.lng != null && Number.isFinite(s.lng) &&
    s.area_m2 != null && s.area_m2 > 0 &&
    s.disposition != null
  );
}

/* -------------------------------------------------------------------------- */
/* Form layout                                                                */
/* -------------------------------------------------------------------------- */

interface Props {
  state: EstimateFormState;
  onChange: (next: EstimateFormState) => void;
  onSubmit: () => void;
  submitting: boolean;
  serverError?: { message: string; field?: keyof EstimateFormState } | null;
  submitLabel?: string;
  estimatedConfidence?: Confidence | null;
}

export default function EstimateForm({
  state,
  onChange,
  onSubmit,
  submitting,
  serverError,
  submitLabel = 'Estimate',
}: Props) {
  const set = <K extends keyof EstimateFormState>(
    key: K, value: EstimateFormState[K],
  ) => onChange({ ...state, [key]: value });

  const valid = useMemo(() => isFormValid(state), [state]);

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (valid && !submitting) onSubmit();
      }}
      className="space-y-7"
    >
      {/* ---------------- Apartment ---------------- */}
      <Section label="Apartment">
        <Row>
          <Field label="Latitude" htmlFor="f-lat" required hint={state.lat == null ? 'required' : null}>
            <NumberInput
              id="f-lat"
              value={state.lat}
              onChange={(v) => set('lat', v)}
              step="0.000001"
              placeholder="50.0875"
            />
          </Field>
          <Field label="Longitude" htmlFor="f-lng" required hint={state.lng == null ? 'required' : null}>
            <NumberInput
              id="f-lng"
              value={state.lng}
              onChange={(v) => set('lng', v)}
              step="0.000001"
              placeholder="14.4205"
            />
          </Field>
        </Row>
        <p className="mt-1 text-[0.7rem] text-[var(--color-ink-4)]">
          Drag the copper pin on the map (right) or click anywhere on it.
        </p>

        <div className="mt-5">
          <Row>
            <Field label="Area" htmlFor="f-area" required hint={!isPositive(state.area_m2) ? 'required, m²' : null}>
              <NumberInput
                id="f-area"
                value={state.area_m2}
                onChange={(v) => set('area_m2', v)}
                step="0.1"
                placeholder="50"
                suffix="m²"
              />
            </Field>
            <Field label="Floor" htmlFor="f-floor">
              <NumberInput
                id="f-floor"
                value={state.floor}
                onChange={(v) => set('floor', v != null ? Math.round(v) : null)}
                step="1"
                placeholder="—"
              />
            </Field>
            <Field label="Of total" htmlFor="f-totalfloors">
              <NumberInput
                id="f-totalfloors"
                value={state.total_floors}
                onChange={(v) => set('total_floors', v != null ? Math.round(v) : null)}
                step="1"
                placeholder="—"
              />
            </Field>
          </Row>
        </div>

        <div className="mt-5">
          <FieldHeader required hint={state.disposition == null ? 'required' : null}>
            Disposition
          </FieldHeader>
          <DispositionPicker
            value={state.disposition}
            onChange={(v) => set('disposition', v)}
          />
        </div>
      </Section>

      {/* ---------------- Building ---------------- */}
      <Section label="Building & condition">
        <Row>
          <Field label="Building type" htmlFor="f-btype">
            <TextInput
              id="f-btype"
              value={state.building_type ?? ''}
              onChange={(v) => set('building_type', v || null)}
              placeholder="—"
            />
          </Field>
          <Field label="Condition" htmlFor="f-cond">
            <TextInput
              id="f-cond"
              value={state.condition ?? ''}
              onChange={(v) => set('condition', v || null)}
              placeholder="—"
            />
          </Field>
          <Field label="Energy class" htmlFor="f-energy">
            <TextInput
              id="f-energy"
              value={state.energy_rating ?? ''}
              onChange={(v) => set('energy_rating', v || null)}
              placeholder="—"
              mono
            />
          </Field>
        </Row>
      </Section>

      {/* ---------------- Amenities ---------------- */}
      <Section label="Amenities">
        <div className="space-y-2">
          <TriRow label="Balcony"  value={state.has_balcony} onChange={(v) => set('has_balcony', v)} />
          <TriRow label="Lift"     value={state.has_lift}    onChange={(v) => set('has_lift', v)} />
          <TriRow label="Parking"  value={state.has_parking} onChange={(v) => set('has_parking', v)} />
        </div>
        <p className="mt-3 text-[0.7rem] text-[var(--color-ink-4)] leading-relaxed">
          Yes / no values are forwarded to comparables as match constraints.
          Leave on <em>any</em> to ignore the attribute when finding comparables.
        </p>
      </Section>

      {/* ---------------- Yield ---------------- */}
      <Section label="Yield">
        <Field label="Purchase price" htmlFor="f-price">
          <NumberInput
            id="f-price"
            value={state.purchase_price_czk}
            onChange={(v) =>
              set('purchase_price_czk', v != null ? Math.round(v) : null)
            }
            step="100000"
            placeholder="—"
            suffix="Kč"
          />
        </Field>
        <p className="mt-2 text-[0.7rem] text-[var(--color-ink-4)] leading-relaxed">
          Optional. If set, the result includes gross yield % (annualised
          rent ÷ purchase price).
        </p>
      </Section>

      {/* ---------------- Advanced ---------------- */}
      <details className="group rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)]">
        <summary className="cursor-pointer list-none flex items-center justify-between gap-4 px-4 py-3">
          <span className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
            Advanced search parameters
          </span>
          <span className="text-[0.7rem] tracking-wide text-[var(--color-ink-3)] group-open:hidden">Show</span>
          <span className="text-[0.7rem] tracking-wide text-[var(--color-ink-3)] hidden group-open:inline">Hide</span>
        </summary>
        <div className="px-4 pb-5 pt-2 space-y-5">
          <SingleSlider
            label="Search radius"
            unit="m"
            min={250} max={5000} step={50}
            value={state.radius_m}
            onChange={(v) => set('radius_m', v)}
          />
          <SingleSlider
            label="Area band"
            unit="±%"
            min={5} max={50} step={1}
            value={Math.round(state.area_band_pct * 100)}
            onChange={(v) => set('area_band_pct', v / 100)}
          />
          <div>
            <FieldHeader>Disposition match</FieldHeader>
            <ButtonRow
              options={[
                { value: 'exact', label: 'Exact' },
                { value: 'loose', label: 'Loose' },
                { value: 'any',   label: 'Any'   },
              ]}
              value={state.disposition_match}
              onChange={(v) => set('disposition_match', v)}
            />
          </div>
          <Row>
            <Field label="Max age" htmlFor="f-maxage">
              <NumberInput
                id="f-maxage"
                value={state.max_age_days}
                onChange={(v) => set('max_age_days', Math.max(1, Math.round(v ?? 7)))}
                step="1"
                suffix="days"
              />
            </Field>
            <Field label="Active only">
              <ButtonRow
                options={[
                  { value: true,  label: 'Active' },
                  { value: false, label: 'Any'    },
                ]}
                value={state.active_only}
                onChange={(v) => set('active_only', v)}
              />
            </Field>
          </Row>
        </div>
      </details>

      {/* ---------------- Submit ---------------- */}
      {serverError && (
        <div className="px-3 py-2 rounded-[var(--radius-sm)] border border-[var(--color-brick)]/30 bg-[var(--color-brick-soft)] text-[var(--color-brick)] text-sm">
          {serverError.message}
        </div>
      )}

      <div className="flex items-center gap-3 pt-2">
        <button
          type="submit"
          disabled={!valid || submitting}
          className={[
            'px-5 py-2.5 text-sm rounded-[var(--radius-sm)] border transition-colors',
            !valid || submitting
              ? 'bg-[var(--color-rule-strong)] text-[var(--color-ink-4)] border-[var(--color-rule-strong)] cursor-not-allowed'
              : 'bg-[var(--color-copper)] text-white border-[var(--color-copper)] hover:bg-[var(--color-copper-2)] hover:border-[var(--color-copper-2)]',
          ].join(' ')}
        >
          {submitting ? 'Estimating…' : submitLabel}
        </button>
        {!valid && (
          <span className="text-[0.78rem] text-[var(--color-ink-3)]">
            Latitude, longitude, area, and disposition are required.
          </span>
        )}
      </div>
    </form>
  );
}

/* -------------------------------------------------------------------------- */
/* Layout primitives                                                          */
/* -------------------------------------------------------------------------- */

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <section>
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
        {label}
      </p>
      <div className="mt-3">{children}</div>
    </section>
  );
}

function Row({ children }: { children: React.ReactNode }) {
  return <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">{children}</div>;
}

function Field({
  label, htmlFor, required, hint, children,
}: {
  label: string;
  htmlFor?: string;
  required?: boolean;
  hint?: string | null;
  children: React.ReactNode;
}) {
  return (
    <div>
      <FieldHeader htmlFor={htmlFor} required={required} hint={hint}>
        {label}
      </FieldHeader>
      <div className="mt-1.5">{children}</div>
    </div>
  );
}

function FieldHeader({
  htmlFor, required, hint, children,
}: {
  htmlFor?: string;
  required?: boolean;
  hint?: string | null;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <label
        htmlFor={htmlFor}
        className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]"
      >
        {children}
        {required && <span className="ml-1 text-[var(--color-ink-4)]">·</span>}
      </label>
      {hint && (
        <span className="text-[0.65rem] tracking-wide text-[var(--color-ink-3)]">
          {hint}
        </span>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Inputs                                                                     */
/* -------------------------------------------------------------------------- */

function NumberInput({
  id, value, onChange, step, placeholder, suffix,
}: {
  id?: string;
  value: number | null;
  onChange: (v: number | null) => void;
  step?: string;
  placeholder?: string;
  suffix?: string;
}) {
  return (
    <div className="flex items-stretch gap-2 min-w-0">
      <input
        id={id}
        type="text"
        inputMode="decimal"
        value={value == null ? '' : String(value)}
        placeholder={placeholder}
        step={step}
        onChange={(e) => {
          const raw = e.target.value.trim().replace(',', '.');
          if (raw === '') return onChange(null);
          const n = Number(raw);
          if (Number.isFinite(n)) onChange(n);
        }}
        className="flex-1 min-w-0 px-3 py-2 text-sm font-mono tabular-nums rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)]"
      />
      {suffix && (
        <span className="self-center text-[0.78rem] tracking-wide text-[var(--color-ink-3)]">
          {suffix}
        </span>
      )}
    </div>
  );
}

function TextInput({
  id, value, onChange, placeholder, mono,
}: {
  id?: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  mono?: boolean;
}) {
  return (
    <input
      id={id}
      type="text"
      value={value}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
      className={[
        'w-full min-w-0 px-3 py-2 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)]',
        mono ? 'font-mono tabular-nums' : '',
      ].join(' ')}
    />
  );
}

/* -------------------------------------------------------------------------- */
/* Disposition single-select (button grid, mirrors Filters.tsx)               */
/* -------------------------------------------------------------------------- */

function DispositionPicker({
  value, onChange,
}: {
  value: Disposition | null;
  onChange: (next: Disposition | null) => void;
}) {
  return (
    <div className="grid grid-cols-5 gap-1.5">
      {DISPOSITIONS.map((d) => {
        const on = value === d;
        return (
          <button
            key={d}
            type="button"
            onClick={() => onChange(on ? null : d)}
            aria-pressed={on}
            className={[
              'px-2 py-1.5 text-xs rounded-[var(--radius-sm)] border transition-colors font-mono tabular-nums',
              on
                ? 'bg-[var(--color-copper)] text-white border-[var(--color-copper)]'
                : 'bg-[var(--color-paper-2)] text-[var(--color-ink-2)] border-[var(--color-rule)] hover:border-[var(--color-rule-strong)]',
            ].join(' ')}
          >
            {d}
          </button>
        );
      })}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Tri-state row (any / yes / no) — mirror of Filters.tsx TriRow              */
/* -------------------------------------------------------------------------- */

const TRI_OPTS: ReadonlyArray<{ value: TriValue; label: string }> = [
  { value: 'any', label: 'any' },
  { value: 'yes', label: 'yes' },
  { value: 'no',  label: 'no'  },
];

function TriRow({
  label, value, onChange,
}: {
  label: string;
  value: TriValue;
  onChange: (v: TriValue) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-2">
      <span className="text-sm text-[var(--color-ink-2)]">{label}</span>
      <div className="grid grid-cols-3 gap-0.5 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] p-0.5">
        {TRI_OPTS.map((opt) => {
          const on = value === opt.value;
          return (
            <button
              key={opt.value}
              type="button"
              onClick={() => onChange(opt.value)}
              aria-pressed={on}
              className={[
                'px-2.5 py-0.5 text-[0.7rem] rounded-[var(--radius-xs)] transition-colors',
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
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Generic single-select button row                                           */
/* -------------------------------------------------------------------------- */

function ButtonRow<T extends string | boolean>({
  options, value, onChange,
}: {
  options: ReadonlyArray<{ value: T; label: string }>;
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div className={`grid gap-1`} style={{ gridTemplateColumns: `repeat(${options.length}, minmax(0, 1fr))` }}>
      {options.map((opt) => {
        const on = value === opt.value;
        return (
          <button
            key={String(opt.value)}
            type="button"
            onClick={() => onChange(opt.value)}
            aria-pressed={on}
            className={[
              'px-2 py-1.5 text-xs rounded-[var(--radius-sm)] border transition-colors',
              on
                ? 'bg-[var(--color-copper-soft)] text-[var(--color-copper)] border-[var(--color-copper)]'
                : 'bg-[var(--color-paper-2)] text-[var(--color-ink-3)] border-[var(--color-rule)] hover:text-[var(--color-ink-2)]',
            ].join(' ')}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Single-handle slider (track styled to match the dual-handle one)           */
/* -------------------------------------------------------------------------- */

function SingleSlider({
  label, unit, min, max, step, value, onChange,
}: {
  label: string;
  unit: string;
  min: number;
  max: number;
  step: number;
  value: number;
  onChange: (n: number) => void;
}) {
  const pct = ((value - min) / (max - min)) * 100;
  return (
    <div>
      <div className="flex items-baseline justify-between">
        <FieldHeader>{label}</FieldHeader>
        <span className="font-mono tabular-nums text-[0.78rem] text-[var(--color-ink-2)]">
          {value}
          <span className="ml-1 text-[var(--color-ink-3)]">{unit}</span>
        </span>
      </div>
      <div className="relative h-6 mt-1">
        <div className="absolute inset-x-0 top-1/2 h-0.5 -translate-y-1/2 bg-[var(--color-rule-strong)] rounded-full" />
        <div
          className="absolute top-1/2 h-0.5 -translate-y-1/2 bg-[var(--color-copper)] rounded-full left-0"
          style={{ width: `${pct}%` }}
        />
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={value}
          onChange={(e) => onChange(Number(e.target.value))}
          className="range-slider"
          aria-label={label}
        />
      </div>
    </div>
  );
}

function isPositive(n: number | null | undefined): boolean {
  return n != null && n > 0;
}
