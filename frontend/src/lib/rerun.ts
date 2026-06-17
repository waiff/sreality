import type {
  CreateEstimationIn,
  EstimationProvider,
  EstimationRun,
  Lifecycle,
  TargetSpecIn,
} from '@/lib/types';

/* Re-run helpers shared between the Estimations list and detail pages.
 * Detail page owns the "Adjust & re-run" panel; the list page exposes
 * a minimal "Re-run with same inputs" affordance on failed rows. Both
 * paths build their POST /estimations body through buildRerunPayload
 * so the new row links to its parent via parent_run_id and the audit
 * trail stays consistent. */

export type RerunOverrides = {
  spec?: TargetSpecIn;
  estimate_kind?: 'rent' | 'sale';
  provider?: EstimationProvider;
  lifecycle?: Lifecycle;
  purchase_price_czk?: number | null;
  expected_monthly_rent_czk?: number | null;
};

export interface RerunInput {
  run: EstimationRun;
  overrides?: RerunOverrides;
}

export function canRerun(run: EstimationRun): boolean {
  return run.input_url != null || run.input_spec != null;
}

export function buildRerunPayload(
  run: EstimationRun,
  overrides?: RerunOverrides,
): CreateEstimationIn {
  const estimateKind =
    overrides?.estimate_kind ?? run.estimate_kind ?? 'rent';
  const mode = estimateKind === 'rent' ? 'agent' : 'deterministic';
  const purchasePrice =
    overrides?.purchase_price_czk !== undefined
      ? overrides.purchase_price_czk
      : run.input_purchase_price_czk;
  const expectedRent =
    overrides?.expected_monthly_rent_czk !== undefined
      ? overrides.expected_monthly_rent_czk
      : null;

  const base: CreateEstimationIn = {
    source: 'ui',
    mode,
    estimate_kind: estimateKind,
    parent_run_id: run.id,
    rerun_reason: overrides ? 'adjust' : 'manual',
    purchase_price_czk: purchasePrice,
    expected_monthly_rent_czk: expectedRent,
    /* Carry operator inputs forward on a re-run. The new row stores its
     * own copy; the original stays untouched (audit invariant). */
    special_instructions: run.special_instructions ?? null,
    contextual_text: run.contextual_text ?? null,
    ...(overrides?.provider ? { provider: overrides.provider } : {}),
    ...(overrides?.lifecycle ? { lifecycle: overrides.lifecycle } : {}),
  };

  if (overrides?.spec) {
    return { ...base, spec: overrides.spec };
  }
  if (run.input_url) {
    return { ...base, url: run.input_url };
  }
  return { ...base, spec: (run.input_spec ?? undefined) as TargetSpecIn | undefined };
}
