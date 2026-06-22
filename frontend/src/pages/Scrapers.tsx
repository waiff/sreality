/* Scrapers page — per-portal operational limits (migration 114).
 *
 * One editable card per registry portal (rate / workers / per-run caps / image
 * limits / completeness) plus a Global defaults card. Limits resolve as
 * CLI override > per-portal DB > global > code default; this page edits the
 * per-portal layer (PUT /admin/portals/{source}/limits) and the global layer
 * (app_settings.scraper_limits_global). Saves take effect on the next scrape —
 * no redeploy. Cadence (cron) is NOT editable here yet.
 *
 * No auth on /admin/* per the slice-1 design — the private Railway URL is the
 * security perimeter. We do NOT pass a bearer token.
 */

import { useEffect, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  listPortals,
  updatePortalLimits,
  listAppSettings,
  updateAppSetting,
  type PortalAdminRow,
  type PortalLimitValues,
  type AppSetting,
} from '@/lib/api';
import { fmtAbsolute } from '@/lib/format';
import {
  portalPosture,
  PORTAL_POSTURE_LABEL,
  PORTAL_POSTURE_BLURB,
} from '@/lib/portalPosture';

const GLOBAL_KEY = 'scraper_limits_global';

type FieldDef = { key: keyof PortalLimitValues; label: string; step: string };

const FIELDS: ReadonlyArray<FieldDef> = [
  { key: 'index_rate', label: 'Index rate (req/s)', step: '0.1' },
  { key: 'detail_rate', label: 'Detail rate (req/s)', step: '0.1' },
  { key: 'detail_workers', label: 'Detail workers', step: '1' },
  { key: 'max_detail_per_run', label: 'Max detail / run', step: '1' },
  { key: 'max_detail_per_category', label: 'Max detail / category', step: '1' },
  { key: 'image_workers', label: 'Image workers', step: '1' },
  { key: 'max_image_downloads', label: 'Max image downloads', step: '1' },
  { key: 'suspicious_stop_window', label: 'Suspicious-stop window', step: '1' },
  { key: 'suspicious_stop_threshold', label: 'Suspicious-stop threshold (0–1)', step: '0.01' },
];

export default function Scrapers() {
  const portalsQ = useQuery({ queryKey: ['admin', 'portals'], queryFn: listPortals });
  const settingsQ = useQuery({ queryKey: ['admin', 'app_settings'], queryFn: listAppSettings });
  const globalSetting = settingsQ.data?.data.find((s) => s.key === GLOBAL_KEY) ?? null;

  return (
    <div className="px-6 pt-5 pb-10 max-w-screen-lg mx-auto">
      <header>
        <h1 className="text-2xl leading-tight">Scrapers</h1>
        <p className="mt-1 text-sm text-[var(--color-ink-2)]">
          Per-portal scrape limits. A blank field inherits the global default
          (shown as the placeholder). Saves apply on the next scrape — no
          redeploy. Schedules (cron) are set in code, not here yet.
        </p>
      </header>

      <section className="mt-8">
        <h2 className="text-lg font-medium border-b border-[var(--color-rule)] pb-2 mb-3">
          Global defaults
        </h2>
        {settingsQ.error && <ErrLine message={(settingsQ.error as Error).message} />}
        {globalSetting ? (
          <GlobalCard setting={globalSetting} />
        ) : (
          <p className="text-sm text-[var(--color-ink-3)]">Loading…</p>
        )}
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-medium border-b border-[var(--color-rule)] pb-2 mb-3">
          Portals
        </h2>
        {portalsQ.error && <ErrLine message={(portalsQ.error as Error).message} />}
        {portalsQ.data ? (
          <div className="space-y-3">
            {portalsQ.data.data.map((p) => (
              <PortalCard key={p.source} portal={p} />
            ))}
          </div>
        ) : (
          <p className="text-sm text-[var(--color-ink-3)]">Loading portals…</p>
        )}
      </section>
    </div>
  );
}

function useToast() {
  const [toast, setToast] = useState<{ kind: 'ok' | 'err'; message: string } | null>(null);
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 4000);
    return () => clearTimeout(t);
  }, [toast]);
  return [toast, setToast] as const;
}

function num(v: unknown): string {
  return v === null || v === undefined ? '' : String(v);
}

function PortalCard({ portal }: { portal: PortalAdminRow }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [toast, setToast] = useToast();
  const isScraper = portal.kind === 'scraper';
  const overrides = portal.overrides ?? {};
  const effective = portal.effective ?? {};

  // draft holds the explicit per-portal override values as strings; empty = inherit.
  const [draft, setDraft] = useState<Record<string, string>>(() =>
    Object.fromEntries(FIELDS.map((f) => [f.key, num(overrides[f.key])])),
  );

  const mutation = useMutation({
    mutationFn: (patch: PortalLimitValues) => updatePortalLimits(portal.source, patch),
    onSuccess: () => {
      setToast({ kind: 'ok', message: 'Saved.' });
      queryClient.invalidateQueries({ queryKey: ['admin', 'portals'] });
    },
    onError: (err: Error) => setToast({ kind: 'err', message: err.message }),
  });

  const save = () => {
    const patch: PortalLimitValues = {};
    for (const f of FIELDS) {
      const raw = draft[f.key].trim();
      if (raw === '') continue; // blank = inherit; merge endpoint leaves it alone
      const n = Number(raw);
      if (Number.isNaN(n)) {
        setToast({ kind: 'err', message: `${f.label}: not a number` });
        return;
      }
      patch[f.key] = n;
    }
    if (Object.keys(patch).length === 0) {
      setToast({ kind: 'err', message: 'Nothing to save (all blank).' });
      return;
    }
    mutation.mutate(patch);
  };

  return (
    <div className="border border-[var(--color-rule)] rounded-[var(--radius-sm)] bg-[var(--color-paper)]">
      <button
        type="button"
        className="w-full px-4 py-3 flex items-baseline justify-between gap-4 text-left"
        onClick={() => setOpen((v) => !v)}
      >
        <div className="flex items-baseline gap-2 flex-wrap">
          <span className="font-medium">{portal.label}</span>
          <span className="font-mono text-[0.7rem] text-[var(--color-ink-4)]">{portal.source}</span>
          <Tag>{portal.kind}</Tag>
          <span
            title={PORTAL_POSTURE_BLURB[portalPosture(portal)]}
            className="text-[0.7rem] text-[var(--color-ink-4)]"
          >
            {PORTAL_POSTURE_LABEL[portalPosture(portal)]}
          </span>
        </div>
        <span className="text-[0.7rem] text-[var(--color-ink-4)]" aria-hidden="true">
          {open ? '▴' : '▾'}
        </span>
      </button>
      {open && (
        <div className="px-4 pt-2 pb-4 border-t border-[var(--color-rule-soft)] space-y-3">
          {!isScraper ? (
            <p className="text-sm text-[var(--color-ink-3)]">
              Not a scheduled scraper (on-demand parser only) — scrape limits don’t apply.
            </p>
          ) : (
            <>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-2">
                {FIELDS.map((f) => {
                  const isOverridden = overrides[f.key] !== null && overrides[f.key] !== undefined;
                  return (
                    <label key={f.key} className="flex items-center justify-between gap-3 text-sm">
                      <span className="text-[var(--color-ink-2)]">{f.label}</span>
                      <span className="flex items-center gap-2">
                        {!isOverridden && (
                          <span className="text-[0.65rem] text-[var(--color-ink-4)]">from&nbsp;global</span>
                        )}
                        <input
                          type="number"
                          step={f.step}
                          value={draft[f.key]}
                          placeholder={num(effective[f.key])}
                          onChange={(e) =>
                            setDraft((d) => ({ ...d, [f.key]: e.target.value }))
                          }
                          className="w-24 px-2 py-1 text-right font-mono text-xs rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] focus:outline-none focus:border-[var(--color-copper)]"
                        />
                      </span>
                    </label>
                  );
                })}
              </div>
              <div className="flex items-center gap-3">
                <button
                  type="button"
                  disabled={mutation.isPending}
                  className="px-3 py-1.5 text-sm rounded-[var(--radius-xs)] bg-[var(--color-copper)] text-[var(--color-paper)] disabled:opacity-60"
                  onClick={save}
                >
                  {mutation.isPending ? 'Saving…' : 'Save'}
                </button>
                <ToastSpan toast={toast} />
              </div>
              <p className="text-[0.7rem] text-[var(--color-ink-4)]">
                Blank = inherit the global default. To re-inherit a set value, clear it via
                the global card (per-portal clear is not supported here yet).
              </p>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function GlobalCard({ setting }: { setting: AppSetting }) {
  const queryClient = useQueryClient();
  const [toast, setToast] = useToast();
  const value = (setting.value ?? {}) as Record<string, unknown>;
  const [draft, setDraft] = useState<Record<string, string>>(() =>
    Object.fromEntries(FIELDS.map((f) => [f.key, num(value[f.key])])),
  );

  const mutation = useMutation({
    mutationFn: (next: Record<string, unknown>) => updateAppSetting(GLOBAL_KEY, next),
    onSuccess: () => {
      setToast({ kind: 'ok', message: 'Saved.' });
      queryClient.invalidateQueries({ queryKey: ['admin', 'app_settings'] });
    },
    onError: (err: Error) => setToast({ kind: 'err', message: err.message }),
  });

  const save = () => {
    const next: Record<string, unknown> = { ...value };
    for (const f of FIELDS) {
      const raw = draft[f.key].trim();
      if (raw === '') {
        next[f.key] = null; // global blank = unlimited / unset for that key
        continue;
      }
      const n = Number(raw);
      if (Number.isNaN(n)) {
        setToast({ kind: 'err', message: `${f.label}: not a number` });
        return;
      }
      next[f.key] = n;
    }
    mutation.mutate(next);
  };

  return (
    <div className="border border-[var(--color-rule)] rounded-[var(--radius-sm)] bg-[var(--color-paper)] px-4 py-3 space-y-3">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-2">
        {FIELDS.map((f) => (
          <label key={f.key} className="flex items-center justify-between gap-3 text-sm">
            <span className="text-[var(--color-ink-2)]">{f.label}</span>
            <input
              type="number"
              step={f.step}
              value={draft[f.key]}
              placeholder="unset"
              onChange={(e) => setDraft((d) => ({ ...d, [f.key]: e.target.value }))}
              className="w-24 px-2 py-1 text-right font-mono text-xs rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] focus:outline-none focus:border-[var(--color-copper)]"
            />
          </label>
        ))}
      </div>
      <div className="flex items-center gap-3">
        <button
          type="button"
          disabled={mutation.isPending}
          className="px-3 py-1.5 text-sm rounded-[var(--radius-xs)] bg-[var(--color-copper)] text-[var(--color-paper)] disabled:opacity-60"
          onClick={save}
        >
          {mutation.isPending ? 'Saving…' : 'Save'}
        </button>
        <ToastSpan toast={toast} />
        <span className="text-[0.7rem] text-[var(--color-ink-4)]">
          {setting.updated_at ? `last edit ${fmtAbsolute(setting.updated_at)}` : ''}
        </span>
      </div>
    </div>
  );
}

function ToastSpan({ toast }: { toast: { kind: 'ok' | 'err'; message: string } | null }) {
  if (!toast) return null;
  return (
    <span
      className={
        toast.kind === 'ok'
          ? 'text-xs text-[var(--color-sage)]'
          : 'text-xs text-[var(--color-brick)]'
      }
    >
      {toast.message}
    </span>
  );
}

function Tag({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-block px-1.5 py-px text-[0.6rem] tracking-[0.08em] uppercase rounded-[var(--radius-xs)] bg-[var(--color-copper-soft)] text-[var(--color-copper)] border border-[var(--color-copper)]/30">
      {children}
    </span>
  );
}

function ErrLine({ message }: { message: string }) {
  return <p className="text-sm text-[var(--color-brick)]">{message}</p>;
}
