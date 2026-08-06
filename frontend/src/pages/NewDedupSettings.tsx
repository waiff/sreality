/* NEW DEDUP · Settings — every knob the simulation engine will use, set up
 * waves ahead of any wave actually running one.
 *
 * Shares its section chrome with the main Settings page (see
 * components/settings/SectionChrome): each category is a numbered-free
 * CollapsibleSection folio (the "L0 · …" label already carries its own
 * ordinal, so no extra index chip), and every explanation collapses to an
 * InfoHint icon unless the page's Compact/Detailed toggle is set to
 * Detailed — in Compact, rows also pack two-up since a bare key + control
 * is short; Detailed drops back to one column so wrapped prose has room.
 */

import { useEffect, useState, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  listNewDedupSettings,
  resetNewDedupSetting,
  updateNewDedupSetting,
  type NewDedupSetting,
} from '@/lib/api';
import { Switch } from '@/components/controls';
import {
  CollapsibleSection,
  InfoHint,
  InfoModeToggle,
  useInfoMode,
  ErrorBanner,
} from '@/components/settings/SectionChrome';

const CATEGORY_ORDER = [
  'general',
  'l0_candidates',
  'l1_exact_attrs',
  'l2_phash',
  'l3_embeddings',
  'l4_vision',
];

const CATEGORY_LABELS: Record<string, string> = {
  general: 'General',
  l0_candidates: 'L0 · Candidate selection',
  l1_exact_attrs: 'L1 · Exact attributes',
  l2_phash: 'L2 · Perceptual hash',
  l3_embeddings: 'L3 · Embeddings',
  l4_vision: 'L4 · Vision',
};

export default function NewDedupSettings() {
  const q = useQuery({
    queryKey: ['new-dedup', 'settings'],
    queryFn: listNewDedupSettings,
  });
  const [infoExpanded, setInfoExpanded] = useInfoMode('new-dedup-settings');

  return (
    <div className="px-6 pt-5 pb-10 max-w-screen-lg mx-auto">
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
        <header>
          <h1 className="text-2xl leading-tight">NEW DEDUP · Settings</h1>
          <p className="mt-1 text-sm text-[var(--color-ink-2)] leading-relaxed max-w-[46rem]">
            Every knob the simulation engine will use, set up waves ahead of any wave actually
            running one. Each default traces to the design's decisions ledger — nothing here is
            a guess. A <em>not yet calibrated</em> tag means the wave that consumes this knob
            hasn't produced a real sample to check it against.
          </p>
        </header>
        <InfoModeToggle expanded={infoExpanded} onChange={setInfoExpanded} />
      </div>

      {q.error && <ErrorBanner message={(q.error as Error).message} />}
      {!q.data && !q.error && (
        <p className="mt-6 text-sm text-[var(--color-ink-3)]">Loading settings…</p>
      )}

      {q.data &&
        CATEGORY_ORDER.map((cat) => {
          const rows = q.data.data.filter((r) => r.category === cat);
          if (!rows.length) return null;
          return (
            <CollapsibleSection
              key={cat}
              id={`new-dedup-${cat}`}
              title={CATEGORY_LABELS[cat] ?? cat}
              infoExpanded={infoExpanded}
            >
              <div className={infoExpanded ? 'space-y-2' : 'grid grid-cols-1 lg:grid-cols-2 gap-2'}>
                {rows.map((s) => (
                  <SettingCard key={s.key} setting={s} infoExpanded={infoExpanded} />
                ))}
              </div>
            </CollapsibleSection>
          );
        })}
    </div>
  );
}

function Badge({ tone, children }: { tone: 'ochre' | 'sage'; children: ReactNode }) {
  return (
    <span
      className={[
        'shrink-0 text-[0.6rem] tracking-[0.1em] uppercase px-1.5 py-0.5 rounded-[var(--radius-xs)]',
        tone === 'ochre'
          ? 'bg-[var(--color-ochre-soft)] text-[var(--color-ochre)]'
          : 'bg-[var(--color-sage-soft)] text-[var(--color-sage)]',
      ].join(' ')}
    >
      {children}
    </span>
  );
}

function SettingCard({ setting, infoExpanded }: { setting: NewDedupSetting; infoExpanded: boolean }) {
  const qc = useQueryClient();
  const [toast, setToast] = useState<{ kind: 'ok' | 'err'; message: string } | null>(null);

  const invalidate = () => qc.invalidateQueries({ queryKey: ['new-dedup', 'settings'] });

  const updateMut = useMutation({
    mutationFn: (value: unknown) => updateNewDedupSetting(setting.key, value),
    onSuccess: () => {
      setToast({ kind: 'ok', message: 'Saved.' });
      invalidate();
    },
    onError: (err: Error) => setToast({ kind: 'err', message: err.message }),
  });
  const resetMut = useMutation({
    mutationFn: () => resetNewDedupSetting(setting.key),
    onSuccess: () => {
      setToast({ kind: 'ok', message: 'Reverted to default.' });
      invalidate();
    },
    onError: (err: Error) => setToast({ kind: 'err', message: err.message }),
  });

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 4000);
    return () => clearTimeout(t);
  }, [toast]);

  return (
    <div className="border border-[var(--color-rule)] rounded-[var(--radius-sm)] bg-[var(--color-paper)] px-3 py-2">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0 flex items-center gap-1.5 flex-wrap">
          <span className="font-mono text-[0.8rem] truncate">{setting.key}</span>
          {!setting.decided && <Badge tone="ochre">not yet calibrated</Badge>}
          {setting.is_override && <Badge tone="sage">edited</Badge>}
          {!infoExpanded && <InfoHint text={setting.explanation} />}
        </div>
        <SettingControl
          setting={setting}
          pending={updateMut.isPending}
          onSave={(value) => updateMut.mutate(value)}
        />
      </div>
      {infoExpanded && (
        <p className="text-xs text-[var(--color-ink-3)] mt-1.5 leading-relaxed">
          {setting.explanation}
        </p>
      )}
      {(setting.is_override || toast) && (
        <div className="flex items-center gap-3 mt-2 pt-2 border-t border-[var(--color-rule-soft)]">
          {setting.is_override && (
            <button
              type="button"
              onClick={() => resetMut.mutate()}
              disabled={resetMut.isPending}
              className="text-xs text-[var(--color-ink-3)] underline decoration-dotted underline-offset-2 hover:text-[var(--color-copper-2)] disabled:opacity-50"
            >
              reset to default ({formatValue(setting.default)})
            </button>
          )}
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
      )}
    </div>
  );
}

function formatValue(value: unknown): string {
  if (typeof value === 'boolean') return value ? 'on' : 'off';
  return String(value);
}

function SettingControl({
  setting,
  pending,
  onSave,
}: {
  setting: NewDedupSetting;
  pending: boolean;
  onSave: (value: unknown) => void;
}) {
  if (setting.value_type === 'boolean') {
    return (
      <Switch
        on={setting.value as boolean}
        pending={pending}
        onChange={(next) => onSave(next)}
        ariaLabel={setting.key}
      />
    );
  }
  if (setting.enum_choices) {
    return (
      <select
        value={String(setting.value)}
        disabled={pending || setting.enum_choices.length <= 1}
        onChange={(e) => onSave(e.target.value)}
        className="px-2 py-1 text-sm rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[var(--color-ink)] shrink-0 disabled:opacity-60"
      >
        {setting.enum_choices.map((choice) => (
          <option key={choice} value={choice}>
            {choice.replace(/_/g, ' ')}
          </option>
        ))}
      </select>
    );
  }
  if (setting.value_type === 'integer' || setting.value_type === 'numeric') {
    return (
      <NumberField
        value={setting.value as number}
        step={setting.value_type === 'integer' ? 1 : 0.01}
        minimum={setting.minimum}
        maximum={setting.maximum}
        pending={pending}
        onSave={onSave}
      />
    );
  }
  return (
    <TextField value={setting.value as string} pending={pending} onSave={onSave} />
  );
}

function NumberField({
  value,
  step,
  minimum,
  maximum,
  pending,
  onSave,
}: {
  value: number;
  step: number;
  minimum: number | null;
  maximum: number | null;
  pending: boolean;
  onSave: (value: number) => void;
}) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  const dirty = draft !== '' && Number(draft) !== value && !Number.isNaN(Number(draft));

  const commit = () => {
    const n = Number(draft);
    if (Number.isNaN(n)) {
      setDraft(String(value));
      return;
    }
    onSave(n);
  };

  return (
    <div className="shrink-0 flex items-center gap-1.5">
      <input
        type="number"
        step={step}
        min={minimum ?? undefined}
        max={maximum ?? undefined}
        value={draft}
        disabled={pending}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') commit();
        }}
        className="w-20 px-2 py-1 font-mono text-sm text-right rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] focus:outline-none focus:border-[var(--color-copper)] disabled:opacity-60"
      />
      {dirty && (
        <button
          type="button"
          onClick={commit}
          disabled={pending}
          className="px-2 py-1 text-xs rounded-[var(--radius-xs)] bg-[var(--color-copper)] text-[var(--color-paper)] disabled:opacity-60"
        >
          Save
        </button>
      )}
    </div>
  );
}

function TextField({
  value,
  pending,
  onSave,
}: {
  value: string;
  pending: boolean;
  onSave: (value: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  const dirty = draft !== value && draft.trim() !== '';

  return (
    <div className="shrink-0 flex items-center gap-1.5">
      <input
        type="text"
        value={draft}
        disabled={pending}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && dirty) onSave(draft);
        }}
        className="w-32 px-2 py-1 font-mono text-sm rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] focus:outline-none focus:border-[var(--color-copper)] disabled:opacity-60"
      />
      {dirty && (
        <button
          type="button"
          onClick={() => onSave(draft)}
          disabled={pending}
          className="px-2 py-1 text-xs rounded-[var(--radius-xs)] bg-[var(--color-copper)] text-[var(--color-paper)] disabled:opacity-60"
        >
          Save
        </button>
      )}
    </div>
  );
}
