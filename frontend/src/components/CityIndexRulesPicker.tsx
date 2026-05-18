/* Phase QUAL — operator-curated city-quality filter widget.
 *
 * Plugs into `<FilterForm customWidgets={...}>` under the registry id
 * `city_index_rules`. Each rule is `{index_name, op, value}`; multiple
 * rules AND. The dropdown enumerates indexes from
 * `city_index_definitions_public` so adding an index to a new CSV
 * upload surfaces here automatically — no frontend change needed.
 */

import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  fetchCityIndexDefinitions,
  type CityIndexDefinition,
} from '@/lib/queries';
import type { CityIndexRule } from '@/lib/filters';

interface Props {
  value: unknown;
  onChange: (next: unknown) => void;
}

/* `op` is locked to `>=` in the UI for now — that's what the user
 * asked for ("metric > 8") and matches the Watchdog default. The data
 * model and SQL helpers carry the operator already so adding the
 * picker is one prop edit when needed. */
const FIXED_OP = '>=' as const;

export default function CityIndexRulesPicker({ value, onChange }: Props) {
  const rules = ((value as CityIndexRule[] | null) ?? []);

  const { data: defs } = useQuery<CityIndexDefinition[], Error>({
    queryKey: ['city_index_definitions'],
    queryFn: fetchCityIndexDefinitions,
    staleTime: Infinity,
    gcTime: Infinity,
  });

  /* Group definitions by category for the dropdown. Overall + category
   * aggregates appear first so the most common filter targets are at
   * the top of the picker. */
  const groups = useMemo(() => groupByCategory(defs ?? []), [defs]);

  const update = (next: CityIndexRule[]) => {
    onChange(next.length === 0 ? null : next);
  };

  const addRule = () => {
    if (!defs || defs.length === 0) return;
    const next: CityIndexRule = {
      index_name: defs[0].index_name,
      op: FIXED_OP,
      value: 7,
    };
    update([...rules, next]);
  };

  const setRule = (i: number, partial: Partial<CityIndexRule>) => {
    update(rules.map((r, idx) => (idx === i ? { ...r, ...partial } : r)));
  };

  const removeRule = (i: number) => {
    update(rules.filter((_, idx) => idx !== i));
  };

  if (!defs) {
    return (
      <p className="text-[0.7rem] text-[var(--color-ink-3)]">
        Loading indexes…
      </p>
    );
  }

  if (defs.length === 0) {
    return (
      <p className="text-[0.7rem] text-[var(--color-ink-3)]">
        No city indexes loaded yet. Run the Seed curated cities workflow.
      </p>
    );
  }

  return (
    <div className="space-y-2">
      {rules.length === 0 && (
        <p className="text-[0.7rem] text-[var(--color-ink-3)]">
          No rules. Listings narrow when a rule is added.
        </p>
      )}
      {rules.map((rule, i) => {
        const def = defs.find((d) => d.index_name === rule.index_name);
        return (
          <div
            key={i}
            className="flex items-center gap-1.5 text-[0.75rem]"
          >
            <select
              className="flex-1 min-w-0 bg-[var(--color-paper-2)] border border-[var(--color-rule)] rounded-[var(--radius-sm)] px-1.5 py-1 text-[0.75rem]"
              value={rule.index_name}
              onChange={(e) => setRule(i, { index_name: e.target.value })}
            >
              {groups.map((g) => (
                <optgroup key={g.label} label={g.label}>
                  {g.defs.map((d) => (
                    <option key={d.index_name} value={d.index_name}>
                      {d.label_en ?? d.label_cs}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
            <span className="font-mono text-[var(--color-ink-3)]">≥</span>
            <input
              type="number"
              className="w-16 bg-[var(--color-paper-2)] border border-[var(--color-rule)] rounded-[var(--radius-sm)] px-1.5 py-1 text-[0.75rem] tabular-nums"
              step={0.1}
              min={def?.scale_min ?? 0}
              max={def?.scale_max ?? 10}
              value={Number.isFinite(rule.value) ? rule.value : 0}
              onChange={(e) => {
                const n = Number(e.target.value);
                if (Number.isFinite(n)) setRule(i, { value: n });
              }}
            />
            <button
              type="button"
              onClick={() => removeRule(i)}
              className="text-[var(--color-ink-3)] hover:text-[var(--color-brick)] px-1"
              title="Remove rule"
              aria-label="Remove rule"
            >
              ×
            </button>
          </div>
        );
      })}
      <button
        type="button"
        onClick={addRule}
        className="text-[0.7rem] tracking-wide uppercase text-[var(--color-copper)] hover:text-[var(--color-ink)] transition-colors"
      >
        + Add rule
      </button>
    </div>
  );
}

const CATEGORY_LABELS: Record<CityIndexDefinition['category'], string> = {
  overall: 'Overall',
  health_env: 'Health & environment',
  material_edu: 'Material & education',
  services_relations: 'Services & relations',
  sub_index: 'Sub-indexes',
};

function groupByCategory(
  defs: ReadonlyArray<CityIndexDefinition>,
): { label: string; defs: ReadonlyArray<CityIndexDefinition> }[] {
  const order: CityIndexDefinition['category'][] = [
    'overall', 'health_env', 'material_edu', 'services_relations', 'sub_index',
  ];
  return order
    .map((cat) => ({
      label: CATEGORY_LABELS[cat],
      defs: defs.filter((d) => d.category === cat),
    }))
    .filter((g) => g.defs.length > 0);
}
