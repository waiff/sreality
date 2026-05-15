/* FilterForm — registry-driven filter renderer.
 *
 * Single component that any consumer (Watchdog, Browse, Settings preview)
 * can drop in to render the operator's filter controls. It reads
 * `FILTER_REGISTRY` from the generated TS file and dispatches on each
 * filter's `ui_control` to one of the shared widget primitives.
 *
 *   <FilterForm
 *     scope="watchdog"
 *     state={spec}
 *     onChange={(id, val) => setSpec({ ...spec, [id]: val })}
 *     visibility={schema?.filters}            // optional matrix override
 *     exclude={['location', 'tags']}          // optional: skip filters
 *   />
 *
 * The dispatcher deliberately does NOT own state. Consumers keep their
 * own (TS-typed) filter state and pass current values + an onChange.
 * State key is the registry id; value type matches the filter's
 * declared `type`.
 *
 * Composite filters (currently just `location`) and any unknown
 * ui_control are skipped. Browse and Watchdog continue to render
 * their existing spatial widgets directly — the LOCATION composite
 * lands in a follow-up commit once the map mode toggle is built.
 */

import {
  FILTER_REGISTRY,
  type Agenda,
  type FilterDef,
} from '@/lib/filterRegistry.generated';
import {
  ControlGroup,
  PickButton,
  NumberCell,
  Section,
  TriRow,
  type TriValue,
} from '@/components/controls';
import {
  MultiselectChips,
  RangeInputs,
  RangeSlider,
  SingleSelectDropdown,
  type EnumOptionLite,
} from '@/components/filter-controls';

export type FilterState = Record<string, unknown>;

interface FilterFormProps {
  scope: Agenda;
  state: FilterState;
  onChange: (id: string, value: unknown) => void;
  /** Optional visibility override. When supplied, only filters with
   *  `visibility[scope] === true` render. When omitted, every filter
   *  the registry declares for `scope` renders (the all-on default). */
  visibility?: ReadonlyArray<{ id: string; visibility: Record<string, boolean> }>;
  /** Ids to skip even if the registry declares them. Use sparingly —
   *  for now, only when the host page renders its own widget for that
   *  filter (e.g. Watchdog's spatial-center inputs). */
  exclude?: ReadonlyArray<string>;
  /** Override widget labels. Keyed by filter id; falls back to a
   *  prettified id. */
  labels?: Record<string, string>;
}

export function FilterForm({
  scope,
  state,
  onChange,
  visibility,
  exclude,
  labels,
}: FilterFormProps) {
  const excludeSet = new Set(exclude ?? []);
  const visibilityById = new Map(
    (visibility ?? []).map((v) => [v.id, v.visibility]),
  );

  const visibleFilters = FILTER_REGISTRY.filters.filter((f) => {
    if (!f.agendas.includes(scope)) return false;
    if (excludeSet.has(f.id)) return false;
    if (visibilityById.size > 0) {
      const cell = visibilityById.get(f.id);
      if (cell && cell[scope] === false) return false;
    }
    return true;
  });

  // Pair min/max sibling filters so the form renders one paired
  // RangeInputs row per pair rather than two separate single-number
  // rows. See `findMaxSibling` for the matching rules.
  const visibleIds = new Set(visibleFilters.map((f) => f.id));
  const pairedAsMin = new Map<string, FilterDef>();   // min id → max def
  const skipAsMax = new Set<string>();
  for (const f of visibleFilters) {
    const sibling = findMaxSibling(f.id, visibleIds);
    if (sibling) {
      const maxDef = FILTER_REGISTRY.filters.find((g) => g.id === sibling);
      if (maxDef) {
        pairedAsMin.set(f.id, maxDef);
        skipAsMax.add(sibling);
      }
    }
  }

  const byCategory = new Map<string, FilterDef[]>();
  for (const f of visibleFilters) {
    if (skipAsMax.has(f.id)) continue;
    const list = byCategory.get(f.category) ?? [];
    list.push(f);
    byCategory.set(f.category, list);
  }

  return (
    <div className="space-y-6">
      {FILTER_REGISTRY.categories
        .filter((c) => byCategory.has(c))
        .map((category) => (
          <ControlGroup key={category} title={category}>
            {byCategory.get(category)!.map((f) => {
              const maxDef = pairedAsMin.get(f.id);
              return (
                <FilterRow
                  key={f.id}
                  def={f}
                  maxDef={maxDef ?? null}
                  value={state[f.id]}
                  maxValue={maxDef ? state[maxDef.id] : undefined}
                  onChange={(v) => onChange(f.id, v)}
                  onChangeMax={
                    maxDef ? (v) => onChange(maxDef.id, v) : undefined
                  }
                  label={labels?.[f.id] ?? prettifyPair(f.id, maxDef?.id)}
                />
              );
            })}
          </ControlGroup>
        ))}
    </div>
  );
}

/** Given a filter id, return the id of its companion max-side, if any. */
function findMaxSibling(id: string, present: Set<string>): string | null {
  if (id.startsWith('min_')) {
    const candidate = 'max_' + id.slice(4);
    return present.has(candidate) ? candidate : null;
  }
  if (id.endsWith('_min')) {
    const candidate = id.slice(0, -4) + '_max';
    return present.has(candidate) ? candidate : null;
  }
  return null;
}

/** Strip the min/max prefix/suffix and prettify what's left so a pair
 *  renders as one shared label. */
function prettifyPair(minId: string, _maxId: string | undefined): string {
  let core = minId;
  if (core.startsWith('min_')) core = core.slice(4);
  if (core.endsWith('_min')) core = core.slice(0, -4);
  return prettifyId(core);
}

/* -------------------------------------------------------------------------- */
/* Per-row dispatch                                                           */
/* -------------------------------------------------------------------------- */

function FilterRow({
  def,
  maxDef,
  value,
  maxValue,
  onChange,
  onChangeMax,
  label,
}: {
  def: FilterDef;
  maxDef: FilterDef | null;
  value: unknown;
  maxValue: unknown;
  onChange: (v: unknown) => void;
  onChangeMax?: (v: unknown) => void;
  label: string;
}) {
  // When paired with a max-side, render the pair as either:
  //   - a dual-thumb RangeSlider when the registry's constraints
  //     declare a complete min + max + step bounds set (typical for
  //     bounded filters like price / area / estate_area), or
  //   - paired RangeInputs (open-ended) when bounds aren't complete.
  //
  // The registry's `ui_control` choice is a preference, not a hard
  // override — adding bounds to a `range_inputs` entry auto-upgrades
  // its UI to a slider without touching this dispatcher. That matches
  // the Browse sidebar's existing slider widget without forcing every
  // surface to opt in by hand.
  if (maxDef && onChangeMax) {
    const c = def.constraints ?? {};
    const hasFullBounds =
      typeof c.min === 'number' &&
      typeof c.max === 'number' &&
      typeof c.step === 'number';
    if (hasFullBounds) {
      return (
        <Section label={label}>
          <RangeSlider
            bounds={{
              min: c.min as number,
              max: c.max as number,
              step: c.step as number,
            }}
            value={[
              (value as number | null) ?? null,
              (maxValue as number | null) ?? null,
            ]}
            onChange={([lo, hi]) => {
              onChange(lo);
              onChangeMax(hi);
            }}
            unit={def.unit ?? undefined}
            ariaLabel={label}
          />
        </Section>
      );
    }
    return (
      <Section label={label}>
        <RangeInputs
          minValue={(value as number | null) ?? null}
          maxValue={(maxValue as number | null) ?? null}
          coerce={def.type === 'int' ? 'int' : 'float'}
          onChange={(lo, hi) => {
            onChange(lo);
            onChangeMax(hi);
          }}
          ariaLabelMin={`${label} min`}
          ariaLabelMax={`${label} max`}
        />
        {def.unit ? <UnitHint unit={def.unit} /> : null}
      </Section>
    );
  }

  switch (def.ui_control) {
    case 'pill_group':
      return (
        <Section label={label}>
          <PillRow def={def} value={value as string | null} onChange={onChange} />
        </Section>
      );

    case 'single_select':
      return (
        <Section label={label}>
          <SingleSelectRow
            def={def}
            value={value as string | number | null}
            onChange={onChange}
          />
        </Section>
      );

    case 'multiselect':
      return (
        <Section label={label}>
          <MultiselectRow
            def={def}
            value={(value as Array<string | number> | null) ?? []}
            onChange={onChange}
          />
        </Section>
      );

    case 'tristate': {
      const tri: TriValue =
        value == null ? 'any' : value ? 'yes' : 'no';
      return (
        <TriRow
          label={label}
          value={tri}
          onChange={(next) => {
            if (next === 'any') onChange(null);
            else onChange(next === 'yes');
          }}
        />
      );
    }

    case 'range_inputs':
    case 'range_slider':
      // Unpaired range filter (no sibling found). Render as a single
      // number input — happens for `min_parking_lots` and any future
      // min-only field. Paired ranges are caught above.
      return (
        <Section label={label}>
          <NumberCell
            value={(value as number | null) ?? null}
            placeholder={def.unit ? `value ${def.unit}` : 'value'}
            onChange={(e) => {
              const raw = e.target.value.trim();
              if (raw === '') {
                onChange(null);
                return;
              }
              const n = Number(raw);
              if (!Number.isFinite(n)) return;
              onChange(def.type === 'int' ? Math.trunc(n) : n);
            }}
          />
        </Section>
      );

    case 'number_input':
      return (
        <Section label={label}>
          <NumberCell
            value={(value as number | null) ?? null}
            placeholder="—"
            onChange={(e) => {
              const raw = e.target.value.trim();
              if (raw === '') {
                onChange(null);
                return;
              }
              const n = Number(raw);
              if (!Number.isFinite(n)) return;
              onChange(def.type === 'int' ? Math.trunc(n) : n);
            }}
          />
          {def.unit ? <UnitHint unit={def.unit} /> : null}
        </Section>
      );

    case 'csv_input':
      return (
        <Section label={label}>
          <CsvRow def={def} value={value} onChange={onChange} />
        </Section>
      );

    case 'boolean':
      return (
        <div className="flex items-center justify-between gap-2">
          <span className="text-sm text-[var(--color-ink-2)]">{label}</span>
          <PickButton
            on={value === true}
            onClick={() => onChange(value === true ? false : true)}
          >
            {value === true ? 'on' : 'off'}
          </PickButton>
        </div>
      );

    case 'location':
      // Composite filter — host page renders its own location widget
      // until the integrated map dot/radius control is built.
      return null;

    default:
      return null;
  }
}

/* -------------------------------------------------------------------------- */
/* Widget adapters                                                            */
/* -------------------------------------------------------------------------- */

function optionsFor(def: FilterDef): EnumOptionLite<string | number>[] {
  if (def.enum_values) {
    return def.enum_values.map((o) => ({
      value: o.value as string | number,
      label: o.label_cs,
    }));
  }
  return [];
}

function PillRow({
  def,
  value,
  onChange,
}: {
  def: FilterDef;
  value: string | null;
  onChange: (v: string | null) => void;
}) {
  const opts = optionsFor(def);
  if (opts.length === 0) return null;
  const cols = opts.length <= 3 ? opts.length : Math.min(opts.length, 4);
  return (
    <div
      className="grid gap-1"
      style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}
    >
      {opts.map((opt) => (
        <PickButton
          key={String(opt.value)}
          on={value === opt.value}
          onClick={() => onChange(value === opt.value ? null : String(opt.value))}
          variant="solid"
        >
          {opt.label}
        </PickButton>
      ))}
    </div>
  );
}

function SingleSelectRow({
  def,
  value,
  onChange,
}: {
  def: FilterDef;
  value: string | number | null;
  onChange: (v: string | number | null) => void;
}) {
  const opts = optionsFor(def);
  return (
    <SingleSelectDropdown
      value={value}
      options={opts}
      onChange={onChange}
    />
  );
}

function MultiselectRow({
  def,
  value,
  onChange,
}: {
  def: FilterDef;
  value: ReadonlyArray<string | number>;
  onChange: (v: Array<string | number> | null) => void;
}) {
  const opts = optionsFor(def);
  if (opts.length === 0) {
    // Free-form list with no closed taxonomy — fall back to CSV.
    return (
      <CsvRow
        def={def}
        value={value}
        onChange={(v) => onChange(v as Array<string | number> | null)}
      />
    );
  }
  return (
    <MultiselectChips
      value={value}
      options={opts}
      onChange={(next) => onChange(next.length === 0 ? null : next)}
      cols={Math.min(opts.length, 5)}
    />
  );
}

function CsvRow({
  def,
  value,
  onChange,
}: {
  def: FilterDef;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const arr = Array.isArray(value) ? value : [];
  const isIntList = def.type === 'int_list';
  return (
    <input
      type="text"
      value={arr.map((x) => String(x)).join(', ')}
      placeholder="comma-separated"
      onChange={(e) => {
        const parts = e.target.value
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean);
        if (parts.length === 0) {
          onChange(null);
          return;
        }
        if (isIntList) {
          const ints = parts
            .map((p) => Number(p))
            .filter((n) => Number.isFinite(n) && n > 0);
          onChange(ints.length === 0 ? null : ints);
        } else {
          onChange(parts);
        }
      }}
      className="w-full px-3 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)]"
    />
  );
}

function UnitHint({ unit }: { unit: string }) {
  return (
    <span className="ml-1 text-[0.7rem] text-[var(--color-ink-4)]">
      {unit}
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* Helpers                                                                    */
/* -------------------------------------------------------------------------- */

function prettifyId(id: string): string {
  // "min_price_czk" → "Min price czk"; "has_balcony" → "Has balcony".
  return id
    .split('_')
    .map((w, i) => (i === 0 ? w[0].toUpperCase() + w.slice(1) : w))
    .join(' ');
}
