import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  listNewDedupSettings,
  resetNewDedupSetting,
  updateNewDedupSetting,
  type NewDedupSetting,
} from '@/lib/api';

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

  return (
    <div className="px-6 py-12 max-w-3xl mx-auto">
      <h1
        className="text-[1.6rem] leading-tight"
        style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
      >
        NEW DEDUP · Settings
      </h1>
      <p className="mt-3 text-sm text-[var(--color-ink-2)] leading-relaxed">
        Every knob the simulation engine will use, set up waves ahead of any wave actually
        running one. Each default traces to the design's decisions ledger — nothing here is
        a guess. A <em>not yet calibrated</em> tag means the wave that consumes this knob
        hasn't produced a real sample to check it against.
      </p>

      {q.error && <ErrorBanner message={(q.error as Error).message} />}
      {!q.data && !q.error && (
        <p className="mt-6 text-sm text-[var(--color-ink-3)]">Loading settings…</p>
      )}

      {q.data &&
        CATEGORY_ORDER.map((cat) => {
          const rows = q.data.data.filter((r) => r.category === cat);
          if (!rows.length) return null;
          return (
            <section key={cat} className="mt-8">
              <span className="block text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] mb-3">
                {CATEGORY_LABELS[cat] ?? cat}
              </span>
              <div className="space-y-3">
                {rows.map((s) => (
                  <SettingCard key={s.key} setting={s} />
                ))}
              </div>
            </section>
          );
        })}
    </div>
  );
}

function SettingCard({ setting }: { setting: NewDedupSetting }) {
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
    <div className="border border-[var(--color-rule)] rounded-[var(--radius-sm)] bg-[var(--color-paper)] px-4 py-3">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-mono text-sm">{setting.key}</span>
            {!setting.decided && (
              <span className="text-[0.6rem] tracking-[0.1em] uppercase px-1.5 py-0.5 rounded-[var(--radius-xs)] bg-[var(--color-ochre-soft)] text-[var(--color-ochre)]">
                not yet calibrated
              </span>
            )}
            {setting.is_override && (
              <span className="text-[0.6rem] tracking-[0.1em] uppercase px-1.5 py-0.5 rounded-[var(--radius-xs)] bg-[var(--color-sage-soft)] text-[var(--color-sage)]">
                edited
              </span>
            )}
          </div>
          <p className="text-xs text-[var(--color-ink-3)] mt-1.5 leading-relaxed">
            {setting.explanation}
          </p>
        </div>
        <SettingControl
          setting={setting}
          pending={updateMut.isPending}
          onSave={(value) => updateMut.mutate(value)}
        />
      </div>
      {(setting.is_override || toast) && (
        <div className="flex items-center gap-3 mt-2.5 pt-2.5 border-t border-[var(--color-rule-soft)]">
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
      <BooleanToggle
        value={setting.value as boolean}
        pending={pending}
        onChange={(next) => onSave(next)}
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

function BooleanToggle({
  value,
  pending,
  onChange,
}: {
  value: boolean;
  pending: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onChange(!value)}
      aria-pressed={value}
      disabled={pending}
      className={[
        'shrink-0 inline-flex items-center justify-center w-9 h-5 rounded-full border transition-colors',
        value
          ? 'bg-[var(--color-sage-soft)] border-[var(--color-sage)]/60'
          : 'bg-[var(--color-paper-2)] border-[var(--color-rule)]',
        pending ? 'opacity-50 cursor-wait' : 'cursor-pointer',
      ].join(' ')}
    >
      <span
        className={[
          'w-3 h-3 rounded-full transition-transform',
          value ? 'translate-x-2 bg-[var(--color-sage)]' : '-translate-x-2 bg-[var(--color-ink-4)]',
        ].join(' ')}
        aria-hidden
      />
    </button>
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

function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="mt-6 p-3 rounded-[var(--radius-sm)] border border-[var(--color-brick)]/30 bg-[var(--color-brick-soft)] text-sm text-[var(--color-brick)]">
      <strong className="font-medium">Failed:</strong> {message}
    </div>
  );
}
