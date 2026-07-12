import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  adminCreatePlan,
  adminDeletePlan,
  adminListEntitlements,
  adminListPlans,
  adminSetEntitlement,
  adminUpdatePlan,
  type Plan,
} from '@/lib/api';

/* Tier editor (Phase 1 increment 5): which agendas each plan can see, plus
 * per-account plan assignment (manual comps). Admin routes; the tenant nav
 * reads the same plans.agendas map via supabase RLS. */

export const AGENDA_KEYS = [
  'browse',
  'pipeline',
  'estimations',
  'watchdogs',
  'notifications',
  'brokers',
  'collections',
] as const;

const AGENDA_LABELS: Record<string, string> = {
  browse: 'Browse',
  pipeline: 'Pipeline',
  estimations: 'Estimations',
  watchdogs: 'Watchdogs',
  notifications: 'Notifications',
  brokers: 'Brokers',
  collections: 'Collections',
};

const plansKey = ['admin', 'plans'] as const;
const entitlementsKey = ['admin', 'entitlements'] as const;

export default function TiersSection() {
  const qc = useQueryClient();
  const plansQ = useQuery({ queryKey: plansKey, queryFn: adminListPlans });
  const entsQ = useQuery({ queryKey: entitlementsKey, queryFn: adminListEntitlements });
  const [newKey, setNewKey] = useState('');
  const [error, setError] = useState<string | null>(null);

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: plansKey });
    void qc.invalidateQueries({ queryKey: entitlementsKey });
  };
  const onError = (err: unknown) =>
    setError(err instanceof Error ? err.message : 'Request failed');

  const updateMut = useMutation({
    mutationFn: ({ key, body }: { key: string; body: Parameters<typeof adminUpdatePlan>[1] }) =>
      adminUpdatePlan(key, body),
    onSuccess: () => { setError(null); invalidate(); },
    onError,
  });
  const createMut = useMutation({
    mutationFn: (key: string) =>
      adminCreatePlan({ key, name: key.charAt(0).toUpperCase() + key.slice(1) }),
    onSuccess: () => { setError(null); setNewKey(''); invalidate(); },
    onError,
  });
  const deleteMut = useMutation({
    mutationFn: adminDeletePlan,
    onSuccess: () => { setError(null); invalidate(); },
    onError,
  });
  const assignMut = useMutation({
    mutationFn: ({ accountId, plan }: { accountId: string; plan: string }) =>
      adminSetEntitlement(accountId, { plan }),
    onSuccess: () => { setError(null); invalidate(); },
    onError,
  });

  const plans = plansQ.data?.data ?? [];
  const ents = entsQ.data?.data ?? [];

  const toggleAgenda = (plan: Plan, agenda: string) =>
    updateMut.mutate({
      key: plan.key,
      body: { agendas: { ...plan.agendas, [agenda]: plan.agendas[agenda] !== true } },
    });

  return (
    <div className="space-y-6">
      {error && (
        <p className="text-[0.8rem] text-[var(--color-brick)]">{error}</p>
      )}

      <div className="overflow-x-auto">
        <table className="w-full text-[0.8rem]">
          <thead>
            <tr className="text-left text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
              <th className="py-1.5 pr-3 font-normal">Tier</th>
              {AGENDA_KEYS.map((k) => (
                <th key={k} className="py-1.5 px-2 font-normal text-center">
                  {AGENDA_LABELS[k]}
                </th>
              ))}
              <th className="py-1.5 px-2 font-normal text-center">Default</th>
              <th className="py-1.5 pl-2" />
            </tr>
          </thead>
          <tbody>
            {plans.map((p) => (
              <tr key={p.key} className="border-t border-[var(--color-rule-soft)]">
                <td className="py-2 pr-3">
                  <span className="text-[var(--color-ink)]">{p.name}</span>
                  <span className="ml-1.5 text-[0.7rem] text-[var(--color-ink-4)]">{p.key}</span>
                </td>
                {AGENDA_KEYS.map((k) => (
                  <td key={k} className="py-2 px-2 text-center">
                    <input
                      type="checkbox"
                      checked={p.agendas[k] === true}
                      onChange={() => toggleAgenda(p, k)}
                      disabled={updateMut.isPending}
                      aria-label={`${p.name}: ${AGENDA_LABELS[k]}`}
                    />
                  </td>
                ))}
                <td className="py-2 px-2 text-center">
                  <input
                    type="radio"
                    name="default-plan"
                    checked={p.is_default}
                    onChange={() =>
                      !p.is_default &&
                      updateMut.mutate({ key: p.key, body: { is_default: true } })
                    }
                    disabled={updateMut.isPending}
                    aria-label={`${p.name} is the default tier`}
                  />
                </td>
                <td className="py-2 pl-2 text-right">
                  {!p.is_default && (
                    <button
                      type="button"
                      onClick={() => deleteMut.mutate(p.key)}
                      disabled={deleteMut.isPending}
                      className="text-[0.75rem] text-[var(--color-ink-3)] hover:text-[var(--color-brick)]"
                    >
                      Delete
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <form
        className="flex items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          const key = newKey.trim().toLowerCase();
          if (key) createMut.mutate(key);
        }}
      >
        <input
          value={newKey}
          onChange={(e) => setNewKey(e.target.value)}
          placeholder="new tier key (e.g. pro)"
          pattern="[a-z0-9_]{1,40}"
          className="px-2 py-1 text-[0.8rem] border border-[var(--color-rule)] rounded-[var(--radius-xs)] bg-[var(--color-paper)]"
        />
        <button
          type="submit"
          disabled={!newKey.trim() || createMut.isPending}
          className="px-2.5 py-1 text-[0.8rem] border border-[var(--color-rule)] rounded-[var(--radius-xs)] text-[var(--color-ink-2)] hover:bg-[var(--color-paper-2)] disabled:opacity-50"
        >
          Add tier
        </button>
      </form>

      <div>
        <h4 className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)] mb-1.5">
          Accounts
        </h4>
        <table className="w-full text-[0.8rem]">
          <tbody>
            {ents.map((row) => (
              <tr key={row.account_id} className="border-t border-[var(--color-rule-soft)]">
                <td className="py-1.5 pr-3 text-[var(--color-ink-2)]">
                  {row.email ?? row.account_id}
                </td>
                <td className="py-1.5 px-2">
                  <select
                    value={row.plan}
                    onChange={(e) =>
                      assignMut.mutate({ accountId: row.account_id, plan: e.target.value })
                    }
                    disabled={assignMut.isPending}
                    className="px-1.5 py-0.5 text-[0.8rem] border border-[var(--color-rule)] rounded-[var(--radius-xs)] bg-[var(--color-paper)]"
                  >
                    {plans.map((p) => (
                      <option key={p.key} value={p.key}>{p.name}</option>
                    ))}
                  </select>
                  {!row.is_explicit && (
                    <span className="ml-1.5 text-[0.7rem] text-[var(--color-ink-4)]">default</span>
                  )}
                </td>
                <td className="py-1.5 px-2 text-[0.75rem] text-[var(--color-ink-3)]">
                  {row.status}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
