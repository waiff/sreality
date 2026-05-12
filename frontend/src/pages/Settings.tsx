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
  type Skill,
  type AppSetting,
  type AgentTool,
  type SkillUpdate,
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
  const skillsQ = useQuery({ queryKey: ['admin', 'skills'], queryFn: listSkills });
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

  return (
    <div className="space-y-3">
      {skills.length === 0 && (
        <p className="text-sm text-[var(--color-ink-3)]">No skills yet.</p>
      )}
      {skills.map((s) => (
        <SkillCard key={s.name} skill={s} tools={tools} />
      ))}
    </div>
  );
}

function SkillCard({ skill, tools }: { skill: Skill; tools: AgentTool[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border border-[var(--color-rule)] rounded-[var(--radius-sm)] bg-[var(--color-paper)]">
      <button
        type="button"
        className="w-full px-4 py-3 flex items-baseline justify-between gap-4 text-left"
        onClick={() => setOpen((v) => !v)}
      >
        <div>
          <div className="font-medium">{skill.name}</div>
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
