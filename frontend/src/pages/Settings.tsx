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
 *
 * Layout: every section is a numbered CollapsibleSection folio (shared
 * with NewDedupSettings — see components/settings/SectionChrome) and every
 * help paragraph collapses to an InfoHint icon by default. The page-level
 * Compact/Detailed toggle flips every description on the page at once —
 * Detailed reproduces the old always-inline layout for reviewing many
 * settings in one pass.
 */

import { useEffect, useId, useMemo, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  listSkills,
  updateSkill,
  listAppSettings,
  updateAppSetting,
  listAgentTools,
  getConditionScoringRegions,
  updateConditionScoringRegions,
  getClipTaggingRegions,
  updateClipTaggingRegions,
  getFilterSchema,
  setFilterVisibility,
  getRentMapStatus,
  listRentMapRevisions,
  uploadRentMapFile,
  triggerRentMapFetch,
  type Skill,
  type AppSetting,
  type AgentTool,
  type SkillUpdate,
  type Agenda,
  type FilterSchemaEntry,
  type RentMapRevision,
  type RentMapIngestResult,
  type ConditionScoringRegionsPayload,
  type ClipTaggingRegionsPayload,
} from '@/lib/api';
import { fmtAbsolute } from '@/lib/format';
import { useTheme, type ThemeMode } from '@/lib/theme';
import { PickButton, Switch, Field } from '@/components/controls';
import TiersSection from '@/components/TiersSection';
import { useWorkflowDocs, type WorkflowDoc } from '@/lib/workflowDocs';
import {
  CollapsibleSection,
  Chevron,
  InfoHint,
  InfoModeToggle,
  useInfoMode,
  ErrorBanner,
} from '@/components/settings/SectionChrome';

export default function Settings() {
  const [infoExpanded, setInfoExpanded] = useInfoMode('settings');
  return (
    <div className="px-6 pt-5 pb-10 max-w-screen-lg mx-auto">
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
        <header>
          <h1 className="text-2xl leading-tight">Settings</h1>
          <p className="mt-1 text-sm text-[var(--color-ink-2)]">
            Edit agent skills and app settings. Saves take effect on the next
            request — no redeploy. Every change is preserved in history.
          </p>
        </header>
        <InfoModeToggle expanded={infoExpanded} onChange={setInfoExpanded} />
      </div>

      <CollapsibleSection
        id="tiers"
        index={1}
        eyebrow="Billing"
        title="Tiers & agenda visibility"
        infoExpanded={infoExpanded}
        description="Which agendas each plan can see, and which plan each account is on. Accounts without an explicit assignment get the default tier."
      >
        <TiersSection />
      </CollapsibleSection>

      <CollapsibleSection id="skills" index={2} eyebrow="Agent" title="Skills" infoExpanded={infoExpanded}>
        <SkillsSection infoExpanded={infoExpanded} />
      </CollapsibleSection>

      <CollapsibleSection
        id="delivery"
        index={3}
        eyebrow="Alerts"
        title="Delivery"
        infoExpanded={infoExpanded}
        hint="Where watchdog & collection-monitoring alerts reach you, beyond the in-app feed. Pick the channels per watchdog / collection; set the destination address here. (Each channel also needs its transport key on the API service.)"
        description={
          <>
            Where watchdog &amp; collection-monitoring alerts reach you, beyond
            the in-app feed. Pick the channels per watchdog / collection; set the
            destination address here. (Each channel also needs its transport key
            on the API service.)
          </>
        }
      >
        <DeliverySection infoExpanded={infoExpanded} />
      </CollapsibleSection>

      <CollapsibleSection
        id="app-settings"
        index={4}
        eyebrow="Tuning"
        title="App settings"
        infoExpanded={infoExpanded}
        description="Operator-tunable prompts and model names used outside the agent (URL parser, listing summary, image comparison)."
      >
        <AppSettingsSection infoExpanded={infoExpanded} />
      </CollapsibleSection>

      <CollapsibleSection
        id="condition-regions"
        index={5}
        eyebrow="Scoring"
        title="Hodnocení stavu — kraje"
        infoExpanded={infoExpanded}
        description="Condition scoring runs kraj by kraj. Enabling a kraj means the scheduled batch job (every 3 h) starts draining that kraj automatically; the count is how many active listings there still await a condition score."
      >
        <ConditionRegionsSection />
      </CollapsibleSection>

      <CollapsibleSection
        id="clip-regions"
        index={6}
        eyebrow="Tagging"
        title="CLIP tagging — priority kraje"
        infoExpanded={infoExpanded}
        description="CLIP image tagging drains the marked kraje first — tags + embeddings — so their coverage lands before the global sweep. Unmarked = no priority (everything drains newest-first); the count is the kraj's active listings."
      >
        <ClipRegionsSection />
      </CollapsibleSection>

      <CollapsibleSection
        id="rent-map"
        index={7}
        eyebrow="Rent map"
        title="Cenová mapa nájemného (MF)"
        infoExpanded={infoExpanded}
        hint="The Ministry of Finance rent price map feeds the secondary rent reference shown on every rental estimate. It auto-grabs monthly from mf.gov.cz; you can also upload a fresh .xlsx or pull the latest now. Every upload is kept in history; the latest revision is always the one in use."
        description={
          <>
            The Ministry of Finance rent price map feeds the secondary rent
            reference shown on every rental estimate. It auto-grabs monthly from
            mf.gov.cz; you can also upload a fresh <span className="font-mono">.xlsx</span>{' '}
            or pull the latest now. Every upload is kept in history; the latest
            revision is always the one in use.
          </>
        }
      >
        <RentMapSection />
      </CollapsibleSection>

      <CollapsibleSection
        id="filter-availability"
        index={8}
        eyebrow="Filters"
        title="Filter availability"
        infoExpanded={infoExpanded}
        description="One row per filter from the canonical registry; columns are the agendas (Browse, Watchdog, agent tools, …) where that filter can apply. Toggle a cell off to hide the filter from that surface — backend matchers and UI forms both respect the matrix. Default is on everywhere a filter is declared."
      >
        <FilterVisibilitySection infoExpanded={infoExpanded} />
      </CollapsibleSection>

      <CollapsibleSection
        id="workflows"
        index={9}
        eyebrow="Workflows"
        title="GitHub Actions"
        infoExpanded={infoExpanded}
        hint="Every workflow in .github/workflows/ — what it does, when it runs, the parameters you can set when running it manually, and links to its run history and source. This list is generated from the workflow files themselves and is kept in sync automatically (the build fails if a workflow changes without regenerating)."
        description={
          <>
            Every workflow in <span className="font-mono">.github/workflows/</span>{' '}
            — what it does, when it runs, the parameters you can set when running
            it manually, and links to its run history and source. This list is
            generated from the workflow files themselves and is kept in sync
            automatically (the build fails if a workflow changes without
            regenerating).
          </>
        }
      >
        <WorkflowsSection infoExpanded={infoExpanded} />
      </CollapsibleSection>

      <CollapsibleSection id="appearance" index={10} eyebrow="Theme" title="Appearance" infoExpanded={infoExpanded}>
        <ThemeToggle />
      </CollapsibleSection>
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

function SkillsSection({ infoExpanded }: { infoExpanded: boolean }) {
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
    <div className="space-y-2">
      {skills.length === 0 && (
        <p className="text-sm text-[var(--color-ink-3)]">No skills yet.</p>
      )}
      {skills
        .filter((s) => showArchived || s.archived_at == null)
        .map((s) => (
          <SkillCard key={s.name} skill={s} tools={tools} infoExpanded={infoExpanded} />
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

function SkillCard({
  skill,
  tools,
  infoExpanded,
}: {
  skill: Skill;
  tools: AgentTool[];
  infoExpanded: boolean;
}) {
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
        className="w-full px-4 py-2.5 flex items-center justify-between gap-4 text-left"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <div className="min-w-0 flex items-center gap-2">
          <span className={['font-medium', isArchived ? 'text-[var(--color-ink-3)]' : ''].join(' ')}>
            {skill.name}
          </span>
          {isArchived && (
            <span className="inline-block px-1.5 py-px text-[0.6rem] tracking-[0.14em] uppercase rounded-[var(--radius-xs)] bg-[var(--color-paper-2)] text-[var(--color-ink-4)] border border-[var(--color-rule)]">
              archived
            </span>
          )}
          {skill.description && !infoExpanded && <InfoHint text={skill.description} />}
        </div>
        <div className="flex items-center gap-2.5 shrink-0">
          <span className="text-[0.7rem] text-[var(--color-ink-4)] tracking-wide whitespace-nowrap">
            {skill.updated_at ? `edited ${fmtAbsolute(skill.updated_at)}` : ''}
          </span>
          <Chevron open={open} />
        </div>
      </button>
      {infoExpanded && skill.description && (
        <div className="px-4 pb-2.5 text-xs text-[var(--color-ink-3)] leading-relaxed">
          {skill.description}
        </div>
      )}
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
      <Field label="System prompt" as="control">
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
/* Delivery (notification channels)                                      */
/* -------------------------------------------------------------------- */

function DeliverySection({ infoExpanded }: { infoExpanded: boolean }) {
  const q = useQuery({ queryKey: ['admin', 'app_settings'], queryFn: listAppSettings });
  if (q.error) return <ErrorBanner message={q.error.message} />;
  if (!q.data)
    return <p className="text-sm text-[var(--color-ink-3)]">Loading…</p>;
  const val = (key: string) => {
    const s = q.data.data.find((x) => x.key === key);
    return typeof s?.value === 'string' ? s.value : '';
  };
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
      <RecipientField
        settingKey="notification_email_to"
        label="Email recipient"
        placeholder="you@example.com"
        hint="Where watchdog / collection email alerts are sent. Also needs RESEND_API_KEY + EMAIL_FROM on the API service."
        initial={val('notification_email_to')}
        infoExpanded={infoExpanded}
      />
      <RecipientField
        settingKey="notification_telegram_chat_id"
        label="Telegram chat ID"
        placeholder="e.g. 123456789"
        hint="DM your bot once, then paste the numeric chat_id. Also needs TELEGRAM_BOT_TOKEN on the API service."
        initial={val('notification_telegram_chat_id')}
        infoExpanded={infoExpanded}
      />
    </div>
  );
}

function RecipientField({
  settingKey,
  label,
  placeholder,
  hint,
  initial,
  infoExpanded,
}: {
  settingKey: string;
  label: string;
  placeholder: string;
  hint: string;
  initial: string;
  infoExpanded: boolean;
}) {
  const queryClient = useQueryClient();
  const [text, setText] = useState(initial);
  const [toast, setToast] = useState<{ kind: 'ok' | 'err'; message: string } | null>(
    null,
  );

  const mutation = useMutation({
    mutationFn: () => updateAppSetting(settingKey, text.trim()),
    onSuccess: () => {
      setToast({ kind: 'ok', message: 'Saved.' });
      queryClient.invalidateQueries({ queryKey: ['admin', 'app_settings'] });
    },
    onError: (err: Error) => setToast({ kind: 'err', message: err.message }),
  });

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 4000);
    return () => clearTimeout(t);
  }, [toast]);

  const dirty = text.trim() !== initial.trim();

  return (
    <div>
      {/* The <label> wraps ONLY the caption and the input. The Save button used
        * to sit inside it too, so "Save" joined the input's accessible name and
        * a click on the button also fired label activation. */}
      <div className="mt-1 flex items-end gap-2">
        <label className="block flex-1 min-w-0">
          <span className="flex items-center gap-1.5">
            <span className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
              {label}
            </span>
            {!infoExpanded && <InfoHint text={hint} />}
          </span>
          <input
            type="text"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={placeholder}
            className="mt-1 w-full px-3 py-2 text-sm rounded-[var(--radius-sm)] bg-[var(--color-inset)] border border-[var(--color-rule)] text-[var(--color-ink)] placeholder:text-[var(--color-ink-4)] focus:outline-none focus:border-[var(--color-rule-strong)]"
          />
        </label>
        <button
          type="button"
          onClick={() => mutation.mutate()}
          disabled={!dirty || mutation.isPending}
          className="px-3 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)] transition-colors disabled:opacity-50"
        >
          {mutation.isPending ? 'Saving…' : 'Save'}
        </button>
      </div>
      {infoExpanded && <p className="mt-1 text-[0.72rem] text-[var(--color-ink-4)]">{hint}</p>}
      {toast && (
        <p
          className={[
            'mt-1 text-[0.72rem]',
            toast.kind === 'ok'
              ? 'text-[var(--color-sage)]'
              : 'text-[var(--color-brick)]',
          ].join(' ')}
        >
          {toast.message}
        </p>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* App settings                                                          */
/* -------------------------------------------------------------------- */

function AppSettingsSection({ infoExpanded }: { infoExpanded: boolean }) {
  const q = useQuery({ queryKey: ['admin', 'app_settings'], queryFn: listAppSettings });
  if (q.error) return <ErrorBanner message={q.error.message} />;
  if (!q.data) return <p className="text-sm text-[var(--color-ink-3)]">Loading app settings…</p>;
  return (
    <div className="space-y-2">
      {q.data.data.map((setting) => (
        <AppSettingRow key={setting.key} setting={setting} infoExpanded={infoExpanded} />
      ))}
    </div>
  );
}

function AppSettingRow({ setting, infoExpanded }: { setting: AppSetting; infoExpanded: boolean }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [text, setText] = useState<string>(() => JSON.stringify(setting.value, null, 2));
  const [parseError, setParseError] = useState<string | null>(null);
  const [toast, setToast] = useState<{ kind: 'ok' | 'err'; message: string } | null>(null);
  const keyId = useId();

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
        className="w-full px-4 py-2.5 flex items-center justify-between gap-4 text-left"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <div className="min-w-0 flex items-center gap-2">
          <span id={keyId} className="font-mono text-sm">{setting.key}</span>
          {setting.description && !infoExpanded && <InfoHint text={setting.description} />}
        </div>
        <div className="flex items-center gap-2.5 shrink-0">
          <span className="text-[0.7rem] text-[var(--color-ink-4)] tracking-wide whitespace-nowrap">
            {setting.updated_at ? `edited ${fmtAbsolute(setting.updated_at)}` : ''}
          </span>
          <Chevron open={open} />
        </div>
      </button>
      {infoExpanded && setting.description && (
        <div className="px-4 pb-2.5 text-xs text-[var(--color-ink-3)] leading-relaxed">
          {setting.description}
        </div>
      )}
      {open && (
        <div className="px-4 pt-2 pb-4 border-t border-[var(--color-rule-soft)] space-y-3">
          <textarea
            aria-labelledby={keyId}
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
/* Region toggle grid — shared shape behind the two per-kraj tables       */
/* below (condition scoring, CLIP priority). A responsive 1/2/3-column    */
/* grid of compact cells reads far better than a 3-column table for a    */
/* flat list of ~14 kraje.                                                */
/* -------------------------------------------------------------------- */

function RegionToggleGrid({
  rows,
  countLabel,
  toggleLabel,
  pending,
  onToggle,
  footnote,
}: {
  rows: Array<{ id: number; name: string; count: number; on: boolean }>;
  countLabel: string;
  toggleLabel: string;
  pending: boolean;
  onToggle: (id: number, next: boolean) => void;
  footnote: string;
}) {
  return (
    <div>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
        {rows.map((r) => (
          <div
            key={r.id}
            className="flex items-center justify-between gap-3 px-3 py-2 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)]"
          >
            <div className="min-w-0">
              <div className="text-sm truncate">{r.name}</div>
              <div className="text-[0.7rem] text-[var(--color-ink-4)] tabular-nums">
                {r.count.toLocaleString('cs-CZ')} {countLabel}
              </div>
            </div>
            <Switch
              on={r.on}
              pending={pending}
              onChange={(next) => onToggle(r.id, next)}
              ariaLabel={`${toggleLabel} ${r.name}`}
            />
          </div>
        ))}
      </div>
      <p className="mt-2.5 text-[0.7rem] text-[var(--color-ink-4)]">{footnote}</p>
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* Hodnocení stavu — kraje (per-kraj condition-scoring toggles)          */
/* -------------------------------------------------------------------- */

function ConditionRegionsSection() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ['admin', 'condition-regions'],
    queryFn: getConditionScoringRegions,
  });
  const [error, setError] = useState<string | null>(null);

  const mut = useMutation({
    mutationFn: (ids: number[]) => updateConditionScoringRegions(ids),
    onMutate: async (ids: number[]) => {
      setError(null);
      const key = ['admin', 'condition-regions'] as const;
      await qc.cancelQueries({ queryKey: key });
      const prev = qc.getQueryData<{ data: ConditionScoringRegionsPayload }>(key);
      if (prev) {
        const on = new Set(ids);
        qc.setQueryData(key, {
          data: {
            ...prev.data,
            enabled_region_ids: ids,
            regions: prev.data.regions.map((r) => ({
              ...r,
              enabled: on.has(r.id),
            })),
          },
        });
      }
      return { prev };
    },
    onError: (err: Error, _ids, ctx) => {
      if (ctx?.prev) {
        qc.setQueryData(['admin', 'condition-regions'], ctx.prev);
      }
      setError(err.message);
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['admin', 'condition-regions'] });
    },
  });

  if (q.error) return <ErrorBanner message={q.error.message} />;
  if (!q.data) {
    return <p className="text-sm text-[var(--color-ink-3)]">Loading kraje…</p>;
  }

  const { regions, parked_no_geo } = q.data.data;

  const toggle = (id: number, next: boolean) => {
    const current = regions.filter((r) => r.enabled).map((r) => r.id);
    mut.mutate(next ? [...current, id] : current.filter((i) => i !== id));
  };

  return (
    <div>
      <RegionToggleGrid
        rows={regions.map((r) => ({ id: r.id, name: r.name, count: r.unscored_active, on: r.enabled }))}
        countLabel="unscored active"
        toggleLabel="Condition scoring for"
        pending={mut.isPending}
        onToggle={toggle}
        footnote={`${parked_no_geo.toLocaleString('cs-CZ')} unscored active listings carry no kraj (missing coordinates) and are outside every toggle.`}
      />
      {error && <p className="mt-2 text-sm text-[var(--color-brick)]">{error}</p>}
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* CLIP tagging — priority kraje (per-kraj drain priority)               */
/* -------------------------------------------------------------------- */

function ClipRegionsSection() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ['admin', 'clip-regions'],
    queryFn: getClipTaggingRegions,
  });
  const [error, setError] = useState<string | null>(null);

  const mut = useMutation({
    mutationFn: (ids: number[]) => updateClipTaggingRegions(ids),
    onMutate: async (ids: number[]) => {
      setError(null);
      const key = ['admin', 'clip-regions'] as const;
      await qc.cancelQueries({ queryKey: key });
      const prev = qc.getQueryData<{ data: ClipTaggingRegionsPayload }>(key);
      if (prev) {
        const on = new Set(ids);
        qc.setQueryData(key, {
          data: {
            ...prev.data,
            priority_region_ids: ids,
            regions: prev.data.regions.map((r) => ({ ...r, priority: on.has(r.id) })),
          },
        });
      }
      return { prev };
    },
    onError: (err: Error, _ids, ctx) => {
      if (ctx?.prev) qc.setQueryData(['admin', 'clip-regions'], ctx.prev);
      setError(err.message);
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['admin', 'clip-regions'] });
    },
  });

  if (q.error) return <ErrorBanner message={q.error.message} />;
  if (!q.data) {
    return <p className="text-sm text-[var(--color-ink-3)]">Loading kraje…</p>;
  }

  const { regions, parked_no_geo } = q.data.data;

  const toggle = (id: number, next: boolean) => {
    const current = regions.filter((r) => r.priority).map((r) => r.id);
    mut.mutate(next ? [...current, id] : current.filter((i) => i !== id));
  };

  return (
    <div>
      <RegionToggleGrid
        rows={regions.map((r) => ({ id: r.id, name: r.name, count: r.active_listings, on: r.priority }))}
        countLabel="active"
        toggleLabel="CLIP priority for"
        pending={mut.isPending}
        onToggle={toggle}
        footnote={`${parked_no_geo.toLocaleString('cs-CZ')} active listings carry no kraj (missing coordinates); they tag in the global sweep, after the priority kraje.`}
      />
      {error && <p className="mt-2 text-sm text-[var(--color-brick)]">{error}</p>}
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* Rent map (MF Cenová mapa nájemného)                                   */
/* -------------------------------------------------------------------- */

function RentMapSection() {
  const qc = useQueryClient();
  const statusQ = useQuery({
    queryKey: ['admin', 'rentmap'],
    queryFn: getRentMapStatus,
  });
  const revsQ = useQuery({
    queryKey: ['admin', 'rentmap', 'revisions'],
    queryFn: listRentMapRevisions,
  });
  const [busy, setBusy] = useState<'upload' | 'fetch' | null>(null);
  const [result, setResult] = useState<RentMapIngestResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['admin', 'rentmap'] });
  };

  const uploadMut = useMutation({
    mutationFn: (file: File) => uploadRentMapFile(file),
    onMutate: () => { setBusy('upload'); setError(null); setResult(null); },
    onSuccess: (r) => { setResult(r); refresh(); },
    onError: (e: unknown) =>
      setError(e instanceof Error ? e.message : 'Upload failed'),
    onSettled: () => setBusy(null),
  });

  const fetchMut = useMutation({
    mutationFn: () => triggerRentMapFetch(),
    onMutate: () => { setBusy('fetch'); setError(null); setResult(null); },
    onSuccess: (r) => { setResult(r); refresh(); },
    onError: (e: unknown) =>
      setError(e instanceof Error ? e.message : 'Fetch failed'),
    onSettled: () => setBusy(null),
  });

  const current: RentMapRevision | null = statusQ.data?.current ?? null;
  const revisions = revsQ.data?.data ?? [];

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="border border-[var(--color-rule)] rounded-[var(--radius-sm)] p-4 bg-[var(--color-paper)]">
          <div className="text-xs tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
            Current revision
          </div>
          {current ? (
            <div className="mt-2 text-sm">
              <span className="font-medium">{current.source_date ?? '—'}</span>{' '}
              <span className="text-[var(--color-ink-3)]">
                · {current.row_count.toLocaleString('cs-CZ')} territories ·{' '}
                {current.source_filename}
              </span>
              <div className="text-xs text-[var(--color-ink-3)] mt-0.5">
                ingested{' '}
                {current.uploaded_at ? fmtAbsolute(current.uploaded_at) : '—'}
                {current.uploaded_by ? ` by ${current.uploaded_by}` : ''}
              </div>
            </div>
          ) : (
            <div className="mt-2 text-sm text-[var(--color-ink-3)]">
              No revision ingested yet.
            </div>
          )}
        </div>

        <div className="border border-[var(--color-rule)] rounded-[var(--radius-sm)] p-4 bg-[var(--color-paper)]">
          <div className="text-xs tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
            Actions
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-3">
            <label className="inline-flex items-center gap-2 text-sm cursor-pointer border border-[var(--color-rule)] rounded-[var(--radius-sm)] px-3 py-2">
              <span>Upload .xlsx</span>
              <input
                type="file"
                accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                className="hidden"
                disabled={busy !== null}
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) uploadMut.mutate(f);
                  e.target.value = '';
                }}
              />
            </label>
            <button
              type="button"
              className="text-sm border border-[var(--color-rule)] rounded-[var(--radius-sm)] px-3 py-2 disabled:opacity-50"
              disabled={busy !== null}
              onClick={() => fetchMut.mutate()}
            >
              {busy === 'fetch' ? 'Fetching…' : 'Fetch latest from MF'}
            </button>
            {busy === 'upload' && (
              <span className="text-sm text-[var(--color-ink-3)]">Uploading…</span>
            )}
          </div>
          {result && (
            <p className="mt-2 text-sm text-[var(--color-sage)]">
              {result.ingested
                ? `Ingested revision ${result.source_revision} — ${result.territory_count.toLocaleString('cs-CZ')} territories (${result.source_date ?? '—'}).`
                : `No change — this file (sha ${result.file_sha256.slice(0, 8)}) was already ingested.`}
            </p>
          )}
          {error && <p className="mt-2 text-sm text-[var(--color-brick)]">{error}</p>}
        </div>
      </div>

      <div>
        <div className="text-xs tracking-[0.18em] uppercase text-[var(--color-ink-3)] mb-2">
          History
        </div>
        {revisions.length === 0 ? (
          <p className="text-sm text-[var(--color-ink-3)]">No revisions yet.</p>
        ) : (
          <table className="w-full text-sm border border-[var(--color-rule)]">
            <thead>
              <tr className="text-left text-xs uppercase tracking-[0.1em] text-[var(--color-ink-3)] border-b border-[var(--color-rule)]">
                <th className="px-3 py-2 font-medium">Rev</th>
                <th className="px-3 py-2 font-medium">Source date</th>
                <th className="px-3 py-2 font-medium">Territories</th>
                <th className="px-3 py-2 font-medium">File</th>
                <th className="px-3 py-2 font-medium">Ingested</th>
              </tr>
            </thead>
            <tbody>
              {revisions.map((r) => (
                <tr
                  key={r.source_revision}
                  className="border-b border-[var(--color-rule)] last:border-0"
                >
                  <td className="px-3 py-2 tabular-nums">{r.source_revision}</td>
                  <td className="px-3 py-2">{r.source_date ?? '—'}</td>
                  <td className="px-3 py-2 tabular-nums">
                    {r.row_count.toLocaleString('cs-CZ')}
                  </td>
                  <td className="px-3 py-2 font-mono text-xs text-[var(--color-ink-3)] truncate max-w-[14rem]">
                    {r.source_filename}
                  </td>
                  <td className="px-3 py-2 text-xs text-[var(--color-ink-3)]">
                    {r.uploaded_at ? fmtAbsolute(r.uploaded_at) : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* GitHub Actions (generated from .github/workflows/*.yml)               */
/* -------------------------------------------------------------------- */

function triggerLabels(doc: WorkflowDoc): string[] {
  const labels: string[] = [];
  for (const s of doc.schedules) labels.push(s.human);
  if (doc.manual) labels.push('Manual');
  if (doc.onPush) labels.push('On push');
  if (doc.onPullRequest) labels.push('On pull request');
  return labels.length ? labels : ['—'];
}

function WorkflowsSection({ infoExpanded }: { infoExpanded: boolean }) {
  const docsQ = useWorkflowDocs();
  const sorted = [...(docsQ.data ?? [])].sort((a, b) => a.name.localeCompare(b.name));
  if (docsQ.isLoading) {
    return <p className="text-sm text-[var(--color-ink-3)]">Loading workflows…</p>;
  }
  if (docsQ.isError) {
    return (
      <p className="text-sm text-[var(--color-brick)]">
        Could not load the workflow list — {docsQ.error.message}
      </p>
    );
  }
  return (
    <div className="space-y-2">
      {sorted.map((doc) => (
        <WorkflowCard key={doc.filename} doc={doc} infoExpanded={infoExpanded} />
      ))}
    </div>
  );
}

function WorkflowCard({ doc, infoExpanded }: { doc: WorkflowDoc; infoExpanded: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border border-[var(--color-rule)] rounded-[var(--radius-sm)] bg-[var(--color-paper)]">
      <button
        type="button"
        className="w-full px-4 py-2.5 flex items-center justify-between gap-4 text-left"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <div className="min-w-0 flex items-center gap-2 flex-wrap">
          <span className="font-medium">{doc.name}</span>
          <span className="font-mono text-[0.7rem] text-[var(--color-ink-4)]">
            {doc.filename}
          </span>
          {!infoExpanded && <InfoHint text={doc.description} />}
        </div>
        <div className="flex items-center gap-2 whitespace-nowrap shrink-0">
          {triggerLabels(doc).slice(0, 2).map((label) => (
            <TriggerBadge key={label} label={label} />
          ))}
          <Chevron open={open} />
        </div>
      </button>
      {infoExpanded && (
        <div className="px-4 pb-2.5 text-xs text-[var(--color-ink-3)] leading-relaxed">
          {doc.description}
        </div>
      )}
      {open && <WorkflowDetail doc={doc} />}
    </div>
  );
}

function TriggerBadge({ label }: { label: string }) {
  return (
    <span className="inline-block px-1.5 py-px text-[0.6rem] tracking-[0.08em] uppercase rounded-[var(--radius-xs)] bg-[var(--color-copper-soft)] text-[var(--color-copper)] border border-[var(--color-copper)]/30">
      {label}
    </span>
  );
}

function WorkflowDetail({ doc }: { doc: WorkflowDoc }) {
  return (
    <div className="px-4 pt-2 pb-4 border-t border-[var(--color-rule-soft)] space-y-4">
      <p className="text-sm text-[var(--color-ink-2)] leading-relaxed">
        {doc.description}
      </p>

      <Field label="Triggers">
        <ul className="text-sm text-[var(--color-ink-2)] space-y-0.5">
          {doc.schedules.map((s) => (
            <li key={s.cron}>
              Scheduled · {s.human}{' '}
              <span className="font-mono text-xs text-[var(--color-ink-4)]">
                ({s.cron})
              </span>
            </li>
          ))}
          {doc.manual && <li>Manual · run from the Actions tab with the parameters below</li>}
          {doc.onPush && (
            <li>
              On push{doc.paths ? ' (when matching paths change)' : ''}
            </li>
          )}
          {doc.onPullRequest && (
            <li>
              On pull request{doc.paths ? ' (when matching paths change)' : ''}
            </li>
          )}
          {doc.schedules.length === 0 &&
            !doc.manual &&
            !doc.onPush &&
            !doc.onPullRequest && <li className="text-[var(--color-ink-3)]">None declared</li>}
        </ul>
        {doc.paths && (
          <div className="mt-1 text-xs text-[var(--color-ink-3)]">
            Paths:{' '}
            {doc.paths.map((p) => (
              <span
                key={p}
                className="font-mono text-[0.7rem] bg-[var(--color-paper-2)] px-1 py-px rounded-[var(--radius-xs)] mr-1"
              >
                {p}
              </span>
            ))}
          </div>
        )}
      </Field>

      {doc.inputs.length > 0 && (
        <Field label="Parameters (when run manually)">
          <div className="border border-[var(--color-rule)] rounded-[var(--radius-xs)] overflow-x-auto">
            <table className="w-full text-sm border-collapse">
              <thead>
                <tr className="bg-[var(--color-paper-2)] border-b border-[var(--color-rule)] text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]">
                  <th className="text-left px-3 py-1.5 font-medium">Parameter</th>
                  <th className="text-left px-3 py-1.5 font-medium">Type</th>
                  <th className="text-left px-3 py-1.5 font-medium">Default</th>
                  <th className="text-left px-3 py-1.5 font-medium">Description</th>
                </tr>
              </thead>
              <tbody>
                {doc.inputs.map((input) => (
                  <tr
                    key={input.name}
                    className="border-b border-[var(--color-rule-soft)] last:border-b-0 align-top"
                  >
                    <td className="px-3 py-1.5">
                      <span className="font-mono text-[0.78rem]">{input.name}</span>
                      {input.required && (
                        <span className="ml-1 text-[0.6rem] uppercase tracking-wide text-[var(--color-brick)]">
                          required
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-1.5 text-xs text-[var(--color-ink-3)]">
                      {input.type}
                      {input.options && (
                        <div className="mt-0.5 text-[var(--color-ink-4)]">
                          {input.options.join(' | ')}
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-1.5 font-mono text-xs text-[var(--color-ink-3)]">
                      {input.default == null ? '—' : input.default}
                    </td>
                    <td className="px-3 py-1.5 text-xs text-[var(--color-ink-2)] max-w-[24rem]">
                      {input.description || '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Field>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        {doc.secrets.length > 0 && (
          <Field label="Secrets used">
            <div className="flex flex-wrap gap-1">
              {doc.secrets.map((s) => (
                <span
                  key={s}
                  className="font-mono text-[0.7rem] bg-[var(--color-paper-2)] px-1.5 py-px rounded-[var(--radius-xs)] border border-[var(--color-rule-soft)]"
                >
                  {s}
                </span>
              ))}
            </div>
          </Field>
        )}
        <Field label="Run settings">
          <ul className="text-xs text-[var(--color-ink-2)] space-y-0.5">
            {doc.timeoutMinutes != null && (
              <li>Timeout: {doc.timeoutMinutes} min</li>
            )}
            {doc.concurrencyGroup && (
              <li>
                Concurrency: <span className="font-mono">{doc.concurrencyGroup}</span>{' '}
                {doc.cancelInProgress === false
                  ? '(queues, never cancelled)'
                  : doc.cancelInProgress === true
                    ? '(cancels in-progress)'
                    : ''}
              </li>
            )}
            {doc.permissions && <li>Permissions: {doc.permissions}</li>}
            {doc.timeoutMinutes == null &&
              !doc.concurrencyGroup &&
              !doc.permissions && (
                <li className="text-[var(--color-ink-3)]">Defaults</li>
              )}
          </ul>
        </Field>
      </div>

      <div className="flex items-center gap-4 pt-1">
        <a
          href={doc.runsUrl}
          target="_blank"
          rel="noreferrer"
          className="text-sm text-[var(--color-copper)] hover:underline underline-offset-2"
        >
          View run history ↗
        </a>
        <a
          href={doc.sourceUrl}
          target="_blank"
          rel="noreferrer"
          className="text-sm text-[var(--color-copper)] hover:underline underline-offset-2"
        >
          View YAML ↗
        </a>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------- */
/* Filter availability (PR 1 / migration 059)                            */
/* -------------------------------------------------------------------- */

function FilterVisibilitySection({ infoExpanded }: { infoExpanded: boolean }) {
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
                  infoExpanded={infoExpanded}
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
  infoExpanded,
  onToggle,
}: {
  category: string;
  filters: FilterSchemaEntry[];
  agendas: Agenda[];
  pending: Set<string>;
  infoExpanded: boolean;
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
            <div className="flex items-center gap-1.5">
              <span className="font-mono text-[0.78rem] text-[var(--color-ink)]">{f.id}</span>
              {!infoExpanded && <InfoHint text={f.description} />}
            </div>
            {infoExpanded && (
              <div className="mt-0.5 text-[0.7rem] text-[var(--color-ink-3)] max-w-[28rem] leading-snug">
                {f.description}
              </div>
            )}
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
                <Switch
                  on={enabled}
                  pending={isPending}
                  onChange={(next) => onToggle(a, f.id, next)}
                  ariaLabel={`${f.id} on ${a}`}
                />
              </td>
            );
          })}
        </tr>
      ))}
    </>
  );
}

/* -------------------------------------------------------------------- */
/* Shared                                                                */
/* -------------------------------------------------------------------- */

