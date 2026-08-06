/* Sidebar control for the deal-pipeline scope (rule #22) — the fine-grained
 * twin of the Pipeline chip in the preset row.
 *
 * Off → the filter is null and the cohort is the whole market. On → the cohort
 * is the operator's pipeline, and the stage chips narrow it further (no chip
 * selected = every stage). Each chip carries the stage's own badge + colour,
 * the same mark the funnels on the cards render, so "the 9s" here and "the 9s"
 * on a card are visibly the same thing.
 */

import { useQuery } from '@tanstack/react-query';

import PipelineMark from '@/components/PipelineMark';
import { fetchPipelineStages, pipelineKeys } from '@/lib/queries';
import { stageAccent, stageBadge } from '@/lib/pipelineStage';
import type { PipelineScope } from '@/lib/filters';

export function PipelineScopePicker({
  value,
  onChange,
}: {
  value: PipelineScope | null;
  onChange: (next: PipelineScope | null) => void;
}) {
  const stagesQ = useQuery({
    queryKey: pipelineKeys.stages,
    queryFn: fetchPipelineStages,
    staleTime: 60_000,
  });
  const stages = stagesQ.data ?? [];
  const on = value != null;
  const selected = new Set(value?.stage_ids ?? []);

  const toggleStage = (id: number) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onChange({ stage_ids: [...next] });
  };

  return (
    <div>
      <label className="flex items-center gap-2 text-[0.8rem] text-[var(--color-ink-2)]">
        <input
          type="checkbox"
          checked={on}
          onChange={(e) => onChange(e.target.checked ? { stage_ids: [] } : null)}
        />
        Jen nemovitosti v pipeline
      </label>

      {on && stages.length > 0 ? (
        <div className="mt-2">
          <p className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
            Fáze
          </p>
          <ul className="mt-1.5 flex flex-wrap gap-1.5">
            {stages.map((s) => {
              const picked = selected.has(s.id);
              const accent = stageAccent(s);
              return (
                <li key={s.id}>
                  <button
                    type="button"
                    onClick={() => toggleStage(s.id)}
                    aria-pressed={picked}
                    className="inline-flex items-center gap-1.5 rounded-[var(--radius-sm)] border px-2 py-1 text-xs transition-colors"
                    style={{
                      color: accent.fg,
                      borderColor: picked ? accent.fg : 'var(--color-rule)',
                      background: picked ? accent.soft : 'transparent',
                    }}
                  >
                    <PipelineMark filled={picked} badge={stageBadge(s, stages)} />
                    <span>{s.label}</span>
                  </button>
                </li>
              );
            })}
          </ul>
          <p className="mt-2 text-[0.65rem] text-[var(--color-ink-4)]">
            {selected.size === 0
              ? 'Bez výběru se zobrazí všechny fáze.'
              : 'Zobrazí se jen vybrané fáze.'}
          </p>
        </div>
      ) : null}
    </div>
  );
}
