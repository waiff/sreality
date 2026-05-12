/* Three-control picker shared by NewEstimationModal and EstimationDetail's
 * re-run form: mode (deterministic / agent), LLM provider (anthropic /
 * gemini), and skill (whichever rows GET /admin/skills returns).
 *
 * Self-contained — uses its own minimal segmented-control + select markup
 * so it can drop into either an "Advanced" disclosure (NewEstimationModal)
 * or into the existing FieldLabel-driven re-run form (EstimationDetail)
 * without coupling to either surface's local atoms. Defaults are the
 * caller's responsibility (preserving the existing modal/detail defaults
 * matters — operator behaviour shouldn't shift just because the picker is
 * now visible).
 *
 * When mode === 'deterministic', provider/skill controls hide entirely:
 * they're meaningless for that path and showing greyed-out controls would
 * just be confusing.
 */

import type { EstimationMode, EstimationProvider } from '@/lib/types';
import { useSkills } from '@/lib/queries';

export interface RunOptionsValue {
  mode: EstimationMode;
  provider: EstimationProvider;
  skill: string;
}

interface Props {
  value: RunOptionsValue;
  onChange: (next: RunOptionsValue) => void;
  /* When true, hide the mode toggle entirely. Used by surfaces where the
   * mode is dictated by external context — e.g. a sale-kind run that
   * doesn't have an agent skill available yet. */
  lockMode?: boolean;
}

export function RunOptionsPicker({ value, onChange, lockMode }: Props) {
  const skills = useSkills();
  const set = <K extends keyof RunOptionsValue>(
    k: K, v: RunOptionsValue[K],
  ) => onChange({ ...value, [k]: v });

  return (
    <div className="flex flex-col gap-3">
      {!lockMode && (
        <div>
          <Label>Mode</Label>
          <Seg
            options={[
              { value: 'deterministic', label: 'Deterministic' },
              { value: 'agent',         label: 'Agent (Claude/Gemini)' },
            ]}
            value={value.mode}
            onChange={(v) => set('mode', v as EstimationMode)}
          />
          <Hint>
            {value.mode === 'agent'
              ? 'Agent reasons through 10–20 turns. Slower (30s–4min) but adapts the cohort search.'
              : 'Deterministic recipe. Fast and predictable; ignores provider/skill.'}
          </Hint>
        </div>
      )}

      {value.mode === 'agent' && (
        <>
          <div>
            <Label>Model provider</Label>
            <Seg
              options={[
                { value: 'anthropic', label: 'Claude' },
                { value: 'gemini',    label: 'Gemini' },
              ]}
              value={value.provider}
              onChange={(v) => set('provider', v as EstimationProvider)}
            />
          </div>

          <div>
            <Label>Skill</Label>
            <select
              value={value.skill}
              onChange={(e) => set('skill', e.target.value)}
              disabled={skills.isLoading || !!skills.error}
              className="mt-1.5 w-full px-3 py-2 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] focus:outline-none focus:border-[var(--color-rule-strong)] disabled:opacity-60"
            >
              {skills.isLoading && <option>Loading…</option>}
              {skills.error && (
                <option>Error loading skills</option>
              )}
              {skills.data?.map((s) => (
                <option key={s.name} value={s.name}>
                  {s.name}
                </option>
              ))}
              {/* Keep the current value selectable even if the skills
                  list didn't include it (older skill that was renamed,
                  or admin endpoint unreachable). */}
              {!skills.isLoading &&
                !skills.data?.some((s) => s.name === value.skill) && (
                  <option value={value.skill}>{value.skill}</option>
                )}
            </select>
            {skills.data && (
              <Hint>
                {skills.data.find((s) => s.name === value.skill)?.description ||
                  '—'}
              </Hint>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function Label({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
      {children}
    </p>
  );
}

function Hint({ children }: { children: React.ReactNode }) {
  return (
    <p className="mt-1.5 text-[0.7rem] text-[var(--color-ink-4)] leading-relaxed">
      {children}
    </p>
  );
}

function Seg<T extends string>({
  options,
  value,
  onChange,
}: {
  options: ReadonlyArray<{ value: T; label: string }>;
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div
      className="mt-1.5 grid gap-1"
      style={{
        gridTemplateColumns: `repeat(${options.length}, minmax(0, 1fr))`,
      }}
    >
      {options.map((opt) => {
        const on = value === opt.value;
        return (
          <button
            type="button"
            key={opt.value}
            onClick={() => onChange(opt.value)}
            className={[
              'px-3 py-1.5 text-[0.78rem] rounded-[var(--radius-sm)] border transition-colors',
              on
                ? 'bg-[var(--color-copper-soft)] text-[var(--color-copper)] border-[var(--color-copper)]/40'
                : 'bg-[var(--color-inset)] text-[var(--color-ink-2)] border-[var(--color-rule)] hover:border-[var(--color-rule-strong)]',
            ].join(' ')}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}
