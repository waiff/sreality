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
import { groupForPicker, indexLabel } from '@/lib/cityIndexes';

/* Re-exports kept so existing call sites that imported these helpers
 * from this file (the pin popup before the shared module was lifted
 * out) keep working without an import rewrite. The canonical home is
 * `frontend/src/lib/cityIndexes.ts`. */
export { indexLabel } from '@/lib/cityIndexes';

interface Props {
  value: unknown;
  onChange: (next: unknown) => void;
}

const OP_OPTIONS: ReadonlyArray<{
  value: NonNullable<CityIndexRule['op']>;
  glyph: string;
}> = [
  { value: '>=', glyph: '≥' },
  { value: '<=', glyph: '≤' },
  { value: '==', glyph: '=' },
  { value: '>', glyph: '>' },
  { value: '<', glyph: '<' },
  { value: '!=', glyph: '≠' },
];

export default function CityIndexRulesPicker({ value, onChange }: Props) {
  const rules = ((value as CityIndexRule[] | null) ?? []);

  const { data: defs } = useQuery<CityIndexDefinition[], Error>({
    queryKey: ['city_index_definitions'],
    queryFn: fetchCityIndexDefinitions,
    staleTime: Infinity,
    gcTime: Infinity,
  });

  /* Group definitions for the dropdown. A small operator-curated
   * "Pinned" group appears at the top — these are the indexes the
   * operator reaches for most often, prefixed with a dash to make
   * the pinning visually obvious. The rest of the list stays grouped
   * by `category` (Overall / Health / Material / Services / Sub). */
  const groups = useMemo(() => groupForPicker(defs ?? []), [defs]);

  const update = (next: CityIndexRule[]) => {
    onChange(next.length === 0 ? null : next);
  };

  const addRule = () => {
    if (!defs || defs.length === 0) return;
    const next: CityIndexRule = {
      index_name: defs[0].index_name,
      op: '>=',
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
                      {g.prefix}{indexLabel(d)}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
            <select
              className="bg-[var(--color-paper-2)] border border-[var(--color-rule)] rounded-[var(--radius-sm)] px-1.5 py-1 text-[0.75rem] font-mono"
              value={rule.op ?? '>='}
              onChange={(e) =>
                setRule(i, { op: e.target.value as CityIndexRule['op'] })
              }
              aria-label="Comparison operator"
            >
              {OP_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.glyph}
                </option>
              ))}
            </select>
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

