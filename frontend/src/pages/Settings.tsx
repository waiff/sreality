/* Settings page — operator control panel.
 *
 * Two sections:
 *   1. Skills: edit the rental_estimator_v1 (and future) skill rows.
 *      System prompt textarea, allowed_tools checkbox list (sourced
 *      from GET /admin/tools), preferred_model dropdowns per
 *      provider, limits number inputs.
 *   2. App settings: parser / summary / vision prompts, model names,
 *      anything else stashed in the app_settings table. Each value
 *      is treated as a raw JSON-encoded string (the existing column
 *      shape).
 *
 * No auth on /admin/* per the slice-1 design — the private Railway
 * URL is the security perimeter. We do NOT pass a bearer token.
 */

import { useEffect, useMemo, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  listSkills,
  updateSkill,
  listAppSettings,
  updateAppSetting,
  listAgentTools,
  getFilterSchema,
  setFilterVisibility,
  type Skill,
  type AppSetting,
  type AgentTool,
  type SkillUpdate,
  type Agenda,
  type FilterSchemaEntry,
} from '@/lib/api';
import { fmtAbsolute } from '@/lib/format';
import { useTheme, type ThemeMode } from '@/lib/theme';
import { PickButton } from '@/components/controls';

export default function Settings() {
  return (
    <div className="px-6 pt-5 pb-10 max-w-screen-lg mx-auto">
      <header>
        <h1 className="text-2xl leading-tight">Settings</h1>
        <p className="mt-1 text-sm text-[var(--color-ink-2)]">
          Edit agent skills and app settings. Saves take effect on the next
          request — no redeploy. Every change is preserved in history.
        </p>
      </header>

      <section className="mt-8">
        <h2 className="text-lg font-medium border-b border-[var(--color-rule)] pb-2 mb-3">
          Skills
        </h2>
        <SkillsSection />
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-medium border-b border-[var(--color-rule)] pb-2 mb-3">
          App settings
        </h2>
        <p className="text-sm text-[var(--color-ink-3)] mb-3">
          Operator-tunable prompts and model names used outside the agent
          (URL parser, listing summary, image comparison).
        </p>
        <AppSettingsSection />
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-medium border-b border-[var(--color-rule)] pb-2 mb-3">
          Filter availability
        </h2>
        <p className="text-sm text-[var(--color-ink-3)] mb-3">
          One row per filter from the canonical registry; columns are
          the agendas (Browse, Watchdog, agent tools, …) where that
          filter can apply. Toggle a cell off to hide the filter
          from that surface — backend matchers and UI forms both
          respect the matrix. Default is on everywhere a filter is
          declared.
        </p>
        <FilterVisibilitySection />
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-medium border-b border-[var(--color-rule)] pb-2 mb-3">
          Appearance
        </h2>
        <ThemeToggle />
      </section>
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* Appearance                                                            */
/* -------------------------------------------------------------------- */

const THEME_OPTS: ReadonlyArray<{
  value: ThemeMode;
  label: string;
  icon: 'sun' | 'moon' | 'system';
}> = [
  { value: 'light',  label: 'Light',  icon: 'sun'    },
  { value: 'dark',   label: 'Dark',   icon: 'moon'   },
  { value: 'system', label: 'System', icon: 'system' },
];

function ThemeToggle() {
  const [mode, setMode] = useTheme();
  return (
    <div>
      <div className="inline-flex gap-1.5">
        {THEME_OPTS.map((opt) => (
          <PickButton
            key={opt.value}
            on={mode === opt.value}
            onClick={() => setMode(opt.value)}
            className="inline-flex items-center gap-1.5 px-3"
          >
            <ThemeGlyph kind={opt.icon} />
            <span>{opt.label}</span>
          </PickButton>
        ))}
      </div>
      <p className="text-xs text-[var(--color-ink-3)] mt-2.5">
        Light is the default. System follows your OS preference.
      </p>
    </div>
  );
}

function ThemeGlyph({ kind }: { kind: 'sun' | 'moon' | 'system' }) {
  if (kind === 'sun') {
    return (
      <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round">
        <circle cx="8" cy="8" r="2.6" />
        <path d="M8 1.5v1.4M8 13.1v1.4M14.5 8h-1.4M2.9 8H1.5M12.6 3.4l-1 1M4.4 11.6l-1 1M12.6 12.6l-1-1M4.4 4.4l-1-1" />
      </svg>
    );
  }
  if (kind === 'moon') {
    return (
      <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round">
        <path d="M13.2 9.6A5.2 5.2 0 0 1 6.4 2.8a5.4 5.4 0 1 0 6.8 6.8Z" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.4">
      <circle cx="8" cy="8" r="5.4" />
      <path d="M8 2.6v10.8" />
      <path d="M8 2.6a5.4 5.4 0 0 1 0 10.8Z" fill="currentColor" stroke="none" />
    </svg>
  );
}

/* -------------------------------------------------------------------- */
/* Skills                                                                */
/* -------------------------------------------------------------------- */

function SkillsSection() {
  const [showArchived, setShowArchived] = useState(false);
  const skillsQ = useQuery({
    queryKey: ['admin', 'skills', { includeArchived: showArchived }],
    queryFn: () => listSkills({ includeArchived: showArchived }),
  });
  const toolsQ = useQuery({ queryKey: ['admin', 'tools'], queryFn: listAgentTools });

  if (skillsQ.error) {
    return <ErrorBanner message={skillsQ.error.message} />;
  }
  if (toolsQ.error) {
    return <ErrorBanner message={toolsQ.error.message} />;
  }
  if (!skillsQ.data || !toolsQ.data) {
    return <p className="text-sm text-[var(--color-ink-3)]">Loading skills…</p>;
  }

  const skills = skillsQ.data.data;
  const tools = toolsQ.data.data;
  const archivedCount = skills.filter((s) => s.archived_at != null).length;

  return (
    <div className="space-y-3">
      {skills.length === 0 && (
        <p className="text-sm text-[var(--color-ink-3)]">No skills yet.</p>
      )}
      {skills
        .filter((s) => showArchived || s.archived_at == null)
        .map((s) => (
          <SkillCard key={s.name} skill={s} tools={tools} />
        ))}

      <button
        type="button"
        onClick={() => setShowArchived((v) => !v)}
        className="mt-2 text-[0.78rem] tracking-wide text-[var(--color-ink-3)] hover:text-[var(--color-copper)] underline-offset-2 hover:underline"
      >
        {showArchived
          ? 'Hide archived skills'
          : archivedCount > 0
            ? `Show archived skills (${archivedCount})`
            : 'Show archived skills'}
      </button>
    </div>
  );
}

function SkillCard({ skill, tools }: { skill: Skill; tools: AgentTool[] }) {
  const [open, setOpen] = useState(false);
  const isArchived = skill.archived_at != null;
  return (
    <div
      className={[
        'border rounded-[var(--radius-sm)]',
        isArchived
          ? 'border-[var(--color-rule-soft)] bg-[var(--color-paper-2)]/60'
          : 'border-[var(--color-rule)] bg-[var(--color-paper)]',
      ].join(' ')}
    >
      <button
        type="button"
        className="w-full px-4 py-3 flex items-baseline justify-between gap-4 text-left"
        onClick={() => setOpen((v) => !v)}
      >
        <div className="min-w-0">
          <div className="flex items-baseline gap-2">
            <span className={['font-medium', isArchived ? 'text-[var(--color-ink-3)]' : ''].join(' ')}>
              {skill.name}
            </span>
            {isArchived && (
              <span className="inline-block px-1.5 py-px text-[0.6rem] tracking-[0.14em] uppercase rounded-[var(--radius-xs)] bg-[var(--color-paper-2)] text-[var(--color-ink-4)] border border-[var(--color-rule)]">
                archived
              </span>
            )}
          </div>
          <div className="text-xs text-[var(--color-ink-3)] mt-0.5">
            {skill.description}
          </div>
        </div>
        <div className="text-[0.7rem] text-[var(--color-ink-4)] tracking-wide whitespace-nowrap">
          {skill.updated_at ? `last edit ${fmtAbsolute(skill.updated_at)}` : ''}
          {' '}
          <span aria-hidden="true">{open ? '▴' : '▾'}</span>
        </div>
      </button>
      {open && <SkillEditor skill={skill} tools={tools} />}
    </div>
  );
}

function SkillEditor({ skill, tools }: { skill: Skill; tools: AgentTool[] }) {
  const queryClient = useQueryClient();
  const [systemPrompt, setSystemPrompt] = useState(skill.system_prompt);
  const [allowedTools, setAllowedTools] = useState<string[]>(skill.allowed_tools);
  const [models, setModels] = useState<Record<string, string>>(skill.preferred_model);
  const [limits, setLimits] = useState(skill.limits);
  const [toast, setToast] = useState<{ kind: 'ok' | 'err'; message: string } | null>(null);

  const mutation = useMutation({
    mutationFn: (patch: SkillUpdate) => updateSkill(skill.name, patch),
    onSuccess: () => {
      setToast({ kind: 'ok', message: 'Saved.' });
      queryClient.invalidateQueries({ queryKey: ['admin', 'skills'] });
    },
    onError: (err: Error) => {
      setToast({ kind: 'err', message: err.message });
    },
  });

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 4000);
    return () => clearTimeout(t);
  }, [toast]);

  const providers = useMemo(() => Object.keys(skill.preferred_model), [skill]);

  const toggleTool = (name: string) => {
    setAllowedTools((prev) =>
      prev.includes(name) ? prev.filter((n) => n !== name) : [...prev, name],
    );
  };

  return (
    <div className="px-4 pt-2 pb-4 border-t border-[var(--color-rule-soft)] space-y-4">
      <Field label="System prompt">
        <textarea
          className="w-full min-h-[14rem] font-mono text-xs leading-relaxed px-3 py-2 rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] focus:outline-none focus:border-[var(--color-copper)]"
          value={systemPrompt}
          onChange={(e) => setSystemPrompt(e.target.value)}
        />
      </Field>

      <Field label="Allowed tools">
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-1.5">
          {tools.map((tool) => (
            <label
              key={tool.name}
              className="flex items-start gap-2 text-sm cursor-pointer"
            >
              <input
                type="checkbox"
                checked={allowedTools.includes(tool.name)}
                onChange={() => toggleTool(tool.name)}
                className="mt-0.5 accent-[var(--color-copper)]"
              />
              <span>
                <span className="font-mono text-xs">{tool.name}</span>
                <span className="block text-xs text-[var(--color-ink-3)]">
                  {tool.description.slice(0, 110)}
                </span>
              </span>
            </label>
          ))}
        </div>
      </Field>

      <Field label="Preferred model per provider">
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          {providers.map((prov) => (
            <label key={prov} className="text-sm">
              <span className="block text-xs text-[var(--color-ink-3)] mb-1">
                {prov}
              </span>
              <input
                type="text"
                className="w-full px-2 py-1 font-mono text-xs rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] focus:outline-none focus:border-[var(--color-copper)]"
                value={models[prov] ?? ''}
                onChange={(e) =>
                  setModels({ ...models, [prov]: e.target.value })
                }
              />
            </label>
          ))}
        </div>
      </Field>

      <Field label="Loop limits">
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <LimitInput
            label="max_iterations"
            value={limits.max_iterations}
            onChange={(v) => setLimits({ ...limits, max_iterations: v })}
            step={1}
          />
          <LimitInput
            label="max_cost_usd"
            value={limits.max_cost_usd}
            onChange={(v) => setLimits({ ...limits, max_cost_usd: v })}
            step={0.1}
          />
          <LimitInput
            label="wall_clock_timeout_s"
            value={limits.wall_clock_timeout_s}
            onChange={(v) => setLimits({ ...limits, wall_clock_timeout_s: v })}
            step={5}
          />
        </div>
      </Field>

      <div className="flex items-center gap-3">
        <button
          type="button"
          disabled={mutation.isPending}
          className="px-3 py-1.5 text-sm rounded-[var(--radius-xs)] bg-[var(--color-copper)] text-[var(--color-paper)] disabled:opacity-60"
          onClick={() =>
            mutation.mutate({
              system_prompt: systemPrompt,
              allowed_tools: allowedTools,
              preferred_model: models,
              limits,
            })
          }
        >
          {mutation.isPending ? 'Saving…' : 'Save'}
        </button>
        {toast && (
          <span
            className={
              toast.kind === 'ok'
                ? 'text-xs text-[var(--color-sage)]'
                : 'text-xs text-[var(--color-brick)]'
            }
          >
            {toast.message}
          </span>
        )}
      </div>
    </div>
  );
}

function LimitInput({
  label, value, onChange, step,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  step: number;
}) {
  return (
    <label className="text-sm">
      <span className="block text-xs text-[var(--color-ink-3)] mb-1">
        {label}
      </span>
      <input
        type="number"
        step={step}
        className="w-full px-2 py-1 font-mono text-xs rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] focus:outline-none focus:border-[var(--color-copper)]"
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </label>
  );
}

/* -------------------------------------------------------------------- */
/* App settings                                                          */
/* -------------------------------------------------------------------- */

function AppSettingsSection() {
  const q = useQuery({ queryKey: ['admin', 'app_settings'], queryFn: listAppSettings });
  if (q.error) return <ErrorBanner message={q.error.message} />;
  if (!q.data) return <p className="text-sm text-[var(--color-ink-3)]">Loading app settings…</p>;
  return (
    <div className="space-y-3">
      {q.data.data.map((setting) => (
        <AppSettingRow key={setting.key} setting={setting} />
      ))}
    </div>
  );
}

function AppSettingRow({ setting }: { setting: AppSetting }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [text, setText] = useState<string>(() => JSON.stringify(setting.value, null, 2));
  const [parseError, setParseError] = useState<string | null>(null);
  const [toast, setToast] = useState<{ kind: 'ok' | 'err'; message: string } | null>(null);

  const mutation = useMutation({
    mutationFn: (value: unknown) => updateAppSetting(setting.key, value),
    onSuccess: () => {
      setToast({ kind: 'ok', message: 'Saved.' });
      queryClient.invalidateQueries({ queryKey: ['admin', 'app_settings'] });
    },
    onError: (err: Error) => {
      setToast({ kind: 'err', message: err.message });
    },
  });

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 4000);
    return () => clearTimeout(t);
  }, [toast]);

  const save = () => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
      setParseError(null);
    } catch (e) {
      setParseError(e instanceof Error ? e.message : 'invalid JSON');
      return;
    }
    mutation.mutate(parsed);
  };

  return (
    <div className="border border-[var(--color-rule)] rounded-[var(--radius-sm)] bg-[var(--color-paper)]">
      <button
        type="button"
        className="w-full px-4 py-3 flex items-baseline justify-between gap-4 text-left"
        onClick={() => setOpen((v) => !v)}
      >
        <div>
          <div className="font-mono text-sm">{setting.key}</div>
          {setting.description && (
            <div className="text-xs text-[var(--color-ink-3)] mt-0.5">
              {setting.description}
            </div>
          )}
        </div>
        <div className="text-[0.7rem] text-[var(--color-ink-4)] tracking-wide whitespace-nowrap">
          {setting.updated_at ? `last edit ${fmtAbsolute(setting.updated_at)}` : ''}
          {' '}
          <span aria-hidden="true">{open ? '▴' : '▾'}</span>
        </div>
      </button>
      {open && (
        <div className="px-4 pt-2 pb-4 border-t border-[var(--color-rule-soft)] space-y-3">
          <textarea
            className="w-full min-h-[10rem] font-mono text-xs leading-relaxed px-3 py-2 rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] focus:outline-none focus:border-[var(--color-copper)]"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
          {parseError && (
            <p className="text-xs text-[var(--color-brick)]">JSON: {parseError}</p>
          )}
          <div className="flex items-center gap-3">
            <button
              type="button"
              disabled={mutation.isPending}
              className="px-3 py-1.5 text-sm rounded-[var(--radius-xs)] bg-[var(--color-copper)] text-[var(--color-paper)] disabled:opacity-60"
              onClick={save}
            >
              {mutation.isPending ? 'Saving…' : 'Save'}
            </button>
            {toast && (
              <span
                className={
                  toast.kind === 'ok'
                    ? 'text-xs text-[var(--color-sage)]'
                    : 'text-xs text-[var(--color-brick)]'
                }
              >
                {toast.message}
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* Filter availability (PR 1 / migration 059)                            */
/* -------------------------------------------------------------------- */

function FilterVisibilitySection() {
  const qc = useQueryClient();
  const schemaQ = useQuery({
    queryKey: ['admin', 'filter-schema'],
    queryFn: getFilterSchema,
  });

  // Pending writes that haven't returned yet keep optimistic UI feedback.
  const [pending, setPending] = useState<Set<string>>(new Set());

  const mut = useMutation({
    mutationFn: ({
      agenda, filterId, enabled,
    }: {
      agenda: Agenda;
      filterId: string;
      enabled: boolean;
    }) => setFilterVisibility(agenda, filterId, enabled),
    onMutate: async ({ agenda, filterId, enabled }) => {
      const key = ['admin', 'filter-schema'] as const;
      await qc.cancelQueries({ queryKey: key });
      const prev = qc.getQueryData<typeof schemaQ.data>(key);
      if (prev) {
        qc.setQueryData(key, {
          ...prev,
          filters: prev.filters.map((f) =>
            f.id === filterId
              ? { ...f, visibility: { ...f.visibility, [agenda]: enabled } }
              : f,
          ),
        });
      }
      setPending((p) => new Set(p).add(`${agenda}|${filterId}`));
      return { prev };
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.prev) {
        qc.setQueryData(['admin', 'filter-schema'], ctx.prev);
      }
    },
    onSettled: (_data, _err, { agenda, filterId }) => {
      setPending((p) => {
        const next = new Set(p);
        next.delete(`${agenda}|${filterId}`);
        return next;
      });
      qc.invalidateQueries({ queryKey: ['admin', 'filter-schema'] });
    },
  });

  if (schemaQ.error) return <ErrorBanner message={schemaQ.error.message} />;
  if (!schemaQ.data) {
    return <p className="text-sm text-[var(--color-ink-3)]">Loading filter registry…</p>;
  }

  const { agendas, categories, filters } = schemaQ.data;
  const filtersByCategory = new Map<string, FilterSchemaEntry[]>();
  for (const f of filters) {
    const list = filtersByCategory.get(f.category) ?? [];
    list.push(f);
    filtersByCategory.set(f.category, list);
  }

  return (
    <div className="border border-[var(--color-rule)] rounded-[var(--radius-sm)] overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm border-collapse">
          <thead>
            <tr className="bg-[var(--color-paper-2)] border-b border-[var(--color-rule)]">
              <th className="text-left px-3 py-2 font-medium text-[var(--color-ink-2)] sticky left-0 bg-[var(--color-paper-2)]">
                Filter
              </th>
              {agendas.map((a) => (
                <th
                  key={a}
                  className="text-center px-2 py-2 font-medium text-[0.65rem] tracking-[0.16em] uppercase text-[var(--color-ink-3)] min-w-[6rem]"
                >
                  {a}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {categories
              .filter((c) => filtersByCategory.has(c))
              .map((category) => (
                <FilterCategoryRows
                  key={category}
                  category={category}
                  filters={filtersByCategory.get(category)!}
                  agendas={agendas}
                  pending={pending}
                  onToggle={(agenda, filterId, enabled) =>
                    mut.mutate({ agenda, filterId, enabled })
                  }
                />
              ))}
          </tbody>
        </table>
      </div>
      <p className="px-3 py-2 text-[0.7rem] text-[var(--color-ink-4)] border-t border-[var(--color-rule)] bg-[var(--color-paper-2)]/50">
        A dash (—) means the filter doesn't apply to that agenda — the
        registry doesn't declare it there, so there's nothing to toggle.
      </p>
    </div>
  );
}

function FilterCategoryRows({
  category,
  filters,
  agendas,
  pending,
  onToggle,
}: {
  category: string;
  filters: FilterSchemaEntry[];
  agendas: Agenda[];
  pending: Set<string>;
  onToggle: (agenda: Agenda, filterId: string, enabled: boolean) => void;
}) {
  return (
    <>
      <tr className="bg-[var(--color-paper)]/60 border-b border-[var(--color-rule-soft)]">
        <td
          colSpan={agendas.length + 1}
          className="px-3 py-1.5 text-[0.65rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium"
        >
          {category}
        </td>
      </tr>
      {filters.map((f) => (
        <tr key={f.id} className="border-b border-[var(--color-rule-soft)] last:border-b-0">
          <td className="px-3 py-2 align-top sticky left-0 bg-[var(--color-paper)]">
            <div className="font-mono text-[0.78rem] text-[var(--color-ink)]">{f.id}</div>
            <div className="mt-0.5 text-[0.7rem] text-[var(--color-ink-3)] max-w-[28rem] leading-snug">
              {f.description}
            </div>
          </td>
          {agendas.map((a) => {
            const declared = a in f.visibility;
            if (!declared) {
              return (
                <td key={a} className="text-center text-[var(--color-ink-4)] px-2 py-2">
                  —
                </td>
              );
            }
            const enabled = f.visibility[a];
            const isPending = pending.has(`${a}|${f.id}`);
            return (
              <td key={a} className="text-center px-2 py-2">
                <FilterCell
                  enabled={enabled}
                  pending={isPending}
                  onChange={(next) => onToggle(a, f.id, next)}
                />
              </td>
            );
          })}
        </tr>
      ))}
    </>
  );
}

function FilterCell({
  enabled,
  pending,
  onChange,
}: {
  enabled: boolean;
  pending: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onChange(!enabled)}
      aria-pressed={enabled}
      disabled={pending}
      className={[
        'inline-flex items-center justify-center w-9 h-5 rounded-full border transition-colors',
        enabled
          ? 'bg-[var(--color-sage-soft)] border-[var(--color-sage)]/60'
          : 'bg-[var(--color-paper-2)] border-[var(--color-rule)]',
        pending ? 'opacity-50 cursor-wait' : 'cursor-pointer',
      ].join(' ')}
    >
      <span
        className={[
          'w-3 h-3 rounded-full transition-transform',
          enabled
            ? 'translate-x-2 bg-[var(--color-sage)]'
            : '-translate-x-2 bg-[var(--color-ink-4)]',
        ].join(' ')}
        aria-hidden
      />
    </button>
  );
}


/* -------------------------------------------------------------------- */
/* Shared                                                                */
/* -------------------------------------------------------------------- */

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-xs tracking-[0.12em] uppercase text-[var(--color-ink-3)] mb-1.5">
        {label}
      </div>
      {children}
    </div>
  );
}

function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="p-3 rounded-[var(--radius-sm)] border border-[var(--color-brick)]/30 bg-[var(--color-brick-soft)] text-sm text-[var(--color-brick)]">
      <strong className="font-medium">Failed:</strong> {message}
    </div>
  );
}
