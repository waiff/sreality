import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { getDedupSettings, updateDedupSetting, type DedupSetting } from '@/lib/api';

/* The dedup-engine knob panel. Reads the backend registry (one source of truth)
 * and renders it grouped, in the civic-archive system: bordered ledger rows,
 * copper "on" switches, mono tabular thresholds, an ochre "edited" marker so the
 * operator can see at a glance what's been moved off the gated-safe defaults. */
export default function DedupEngineSection() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ['dedup-settings'], queryFn: getDedupSettings });
  const mut = useMutation({
    mutationFn: ({ key, value }: { key: string; value: unknown }) =>
      updateDedupSetting(key, value),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['dedup-settings'] }),
  });

  const groups = useMemo(() => {
    const out: { name: string; items: DedupSetting[] }[] = [];
    for (const s of q.data?.data ?? []) {
      let g = out.find((x) => x.name === s.group);
      if (!g) {
        g = { name: s.group, items: [] };
        out.push(g);
      }
      g.items.push(s);
    }
    return out;
  }, [q.data]);

  if (q.isLoading)
    return <p className="text-sm text-[var(--color-ink-3)]">Loading…</p>;
  if (q.isError)
    return (
      <p className="text-sm text-[var(--color-brick)]">
        Couldn’t load the dedup settings.
      </p>
    );

  return (
    <div className="space-y-6">
      {groups.map((g) => (
        <div key={g.name}>
          <div className="text-xs tracking-[0.18em] uppercase text-[var(--color-ink-3)] mb-1.5">
            {g.name}
          </div>
          <div className="border border-[var(--color-rule)] rounded-[var(--radius-sm)] divide-y divide-[var(--color-rule)] bg-[var(--color-paper)]">
            {g.items.map((s) => (
              <Row
                key={s.key}
                s={s}
                pending={mut.isPending && mut.variables?.key === s.key}
                onChange={(value) => mut.mutate({ key: s.key, value })}
              />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function Row({
  s,
  pending,
  onChange,
}: {
  s: DedupSetting;
  pending: boolean;
  onChange: (v: unknown) => void;
}) {
  return (
    <div className="flex items-start gap-4 px-3.5 py-3">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="text-sm text-[var(--color-ink)]">{s.label}</span>
          {!s.is_default && (
            <span
              title="Changed from the default"
              className="inline-flex items-center gap-1 text-[0.62rem] uppercase tracking-[0.1em] text-[var(--color-ochre)]"
            >
              <span className="w-1.5 h-1.5 rounded-full bg-[var(--color-ochre)]" />
              edited
            </span>
          )}
        </div>
        {s.help && (
          <p className="mt-1 text-[0.78rem] leading-snug text-[var(--color-ink-3)]">
            {s.help}
          </p>
        )}
      </div>
      <div className="shrink-0 pt-0.5">
        <Control s={s} disabled={pending} onChange={onChange} />
      </div>
    </div>
  );
}

function Control({
  s,
  disabled,
  onChange,
}: {
  s: DedupSetting;
  disabled: boolean;
  onChange: (v: unknown) => void;
}) {
  if (s.kind === 'bool') {
    const on = s.value === true;
    return (
      <button
        type="button"
        role="switch"
        aria-checked={on}
        aria-label={s.label}
        disabled={disabled}
        onClick={() => onChange(!on)}
        className={[
          'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-[var(--radius-sm)] border text-[0.78rem] transition-colors disabled:opacity-50',
          on
            ? 'border-[var(--color-copper)] bg-[var(--color-copper-soft)] text-[var(--color-copper)]'
            : 'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)]',
        ].join(' ')}
      >
        <span
          className={[
            'w-1.5 h-1.5 rounded-full',
            on ? 'bg-[var(--color-copper)]' : 'bg-[var(--color-ink-4)]',
          ].join(' ')}
        />
        {on ? 'On' : 'Off'}
      </button>
    );
  }
  return <CommitInput s={s} disabled={disabled} onChange={onChange} />;
}

/* Commit-on-blur (never parse per keystroke): a local draft, validated + pushed
 * on blur / Enter, re-synced when the saved value comes back. */
function CommitInput({
  s,
  disabled,
  onChange,
}: {
  s: DedupSetting;
  disabled: boolean;
  onChange: (v: unknown) => void;
}) {
  const [draft, setDraft] = useState(String(s.value ?? ''));
  useEffect(() => setDraft(String(s.value ?? '')), [s.value]);

  const commit = () => {
    if (s.kind === 'float') {
      const n = Number(draft);
      if (!Number.isFinite(n)) {
        setDraft(String(s.value));
        return;
      }
      if (n !== Number(s.value)) onChange(n);
    } else {
      const v = draft.trim();
      if (!v) {
        setDraft(String(s.value));
        return;
      }
      if (v !== String(s.value)) onChange(v);
    }
  };

  return (
    <input
      value={draft}
      disabled={disabled}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
      }}
      inputMode={s.kind === 'float' ? 'decimal' : 'text'}
      className={[
        'rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-inset)] px-2 py-1 text-sm font-mono tabular-nums text-[var(--color-ink)] focus:outline-none focus:border-[var(--color-copper)] focus:ring-1 focus:ring-[var(--color-focus)] disabled:opacity-50',
        s.kind === 'float' ? 'w-20 text-right' : 'w-44',
      ].join(' ')}
    />
  );
}
