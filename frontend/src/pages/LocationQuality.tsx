/* Location quality — the operator's read of `listing_location`, the location
 * program's one answer table (migration 501). Admin-only; every read goes
 * through the identity-gated `/location/*` API (the location tables are
 * service-role-only). Every precision panel states its grain — mixing listing-
 * and property-grain precision is a lie by aggregation (§5.5.4).
 *
 * W2-b deleted three panels with the columns under them: the pin-sharing
 * histogram and its collision-class list (the epoch that produced them is gone;
 * the shared-pin count is a read-time aggregate W3 computes in the browse_list
 * rebuild, where the map needs it), the position-source / admin-assignment
 * mixes, and the frozen labelled sample (two tables whose purpose was scoring
 * old-vs-new precision, and "new" is the only system now). What is left is the
 * two axes a consumer asks — granularity and match confidence — over a totals
 * row whose centre of gravity is rule 25's invariant: every active Czech
 * listing has a town.
 *
 * Visual note: enum mixes are single-hue horizontal bars — magnitude of one
 * measure, so one hue; identity color would be noise. */

import { useMemo, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Field } from '@/components/controls';
import {
  fetchCorpusSummary, fetchInspector, fetchInspectorByNative, fetchSourceOverview,
  submitCorrection,
  LOCATION_SOURCES,
  type CorrectionResult, type Inspector, type MixRow, type SourceOverview,
} from '../lib/locationQuality';
import { ApiError } from '../lib/api';
import { fmtCount, fmtRelative } from '../lib/format';

/* ---------- shared bits (Costs.tsx idiom) ---------- */

function Card({ title, accessory, children }: {
  title: string; accessory?: React.ReactNode; children: React.ReactNode;
}) {
  return (
    <section className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)]">
      <header className="flex items-center gap-3 border-b border-[var(--color-rule-soft)] px-4 py-2.5">
        <h2 className="text-[0.7rem] tracking-[0.12em] uppercase text-[var(--color-ink-3)]">{title}</h2>
        {accessory ? <div className="ml-auto">{accessory}</div> : null}
      </header>
      <div className="px-4 py-3">{children}</div>
    </section>
  );
}

function Stat({ label, value, hint, accent, danger }: {
  label: string; value: string; hint?: string; accent?: boolean; danger?: boolean;
}) {
  const tone = danger ? 'text-[var(--color-brick)]' : accent ? 'text-[var(--color-copper)]' : '';
  return (
    <div className="rounded-[var(--radius-sm)] border border-[var(--color-rule-soft)] bg-[var(--color-paper-2)] px-3 py-2">
      <div className="text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]">{label}</div>
      <div className={`font-mono tabular-nums text-xl ${tone}`}>{value}</div>
      {hint ? <div className="text-[0.68rem] text-[var(--color-ink-4)]">{hint}</div> : null}
    </div>
  );
}

function BarList({ rows, total }: { rows: MixRow[]; total: number }) {
  if (rows.length === 0) {
    return <p className="text-sm text-[var(--color-ink-3)]">No rows.</p>;
  }
  return (
    <div className="space-y-1.5">
      {rows.map((r) => {
        const pct = total ? (100 * r.n) / total : 0;
        return (
          <div key={r.value ?? 'null'} className="flex items-center gap-2">
            <span className="w-44 shrink-0 truncate text-sm text-[var(--color-ink-2)]">
              {r.value ?? '∅'}
            </span>
            <span className="h-2 flex-1 rounded-full bg-[var(--color-rule-soft)]">
              <span
                className="block h-2 rounded-full bg-[var(--color-copper)]"
                style={{ width: `${Math.max(pct, pct > 0 ? 1 : 0)}%` }}
              />
            </span>
            <span className="text-right font-mono tabular-nums text-[0.78rem]">
              {fmtCount(r.n)}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function errText(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  return e instanceof Error ? e.message : String(e);
}

function ErrorBanner({ error }: { error: unknown }) {
  return (
    <div className="rounded-[var(--radius-sm)] border border-[var(--color-brick)] bg-[var(--color-brick-soft)] px-3 py-2 text-sm text-[var(--color-brick)]">
      {errText(error)}
    </div>
  );
}

/* ---------- source overview panels ---------- */

function OverviewPanels({ ov }: { ov: SourceOverview }) {
  const t = ov.totals;
  const pct = (n: number) => (t.active_rows ? `${((100 * n) / t.active_rows).toFixed(1)}%` : '—');
  return (
    <>
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
        <Stat label="Active rows" value={fmtCount(t.active_rows)} />
        <Stat label="With a town" value={pct(t.with_obec_kod)} hint={`${fmtCount(t.with_obec_kod)} rows`} accent />
        <Stat label="≥ building" value={pct(t.building_or_better)} hint={`${fmtCount(t.building_or_better)} rows`} />
        <Stat label="≥ street" value={pct(t.street_or_better)} hint={`${fmtCount(t.street_or_better)} rows`} />
        <Stat label="kód ADM" value={pct(t.with_adm_kod)} hint={`${fmtCount(t.with_ulice_kod)} with a street code`} />
        <Stat label="Disputed" value={fmtCount(t.disputed)} danger={t.disputed > 0} />
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <Card title="Granularity × count (listing grain)">
          <BarList rows={ov.mixes.granularity ?? []} total={t.active_rows} />
        </Card>
        <Card title="Match confidence">
          <BarList rows={ov.mixes.match_confidence ?? []} total={t.active_rows} />
        </Card>
      </div>

      <div className="mt-4">
        <Card
          title="Registry version"
          accessory={ov.current_registry ? (
            <span className="text-[0.7rem] text-[var(--color-ink-3)]">
              current: <span className="font-mono">{ov.current_registry.label}</span>
            </span>
          ) : undefined}
        >
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
            <Stat
              label="On an older registry"
              value={pct(t.stale_registry)}
              hint={`${fmtCount(t.stale_registry)} rows`}
              danger={t.active_rows > 0 && t.stale_registry > t.active_rows / 2}
            />
          </div>
          <p className="mt-2 text-[0.7rem] text-[var(--color-ink-4)]">
            Rows on an older registry version re-resolve on the next drain after a registry load —
            a growing stale share means the drain is behind, not that data got worse.
          </p>
        </Card>
      </div>
    </>
  );
}

/* ---------- inspector + corrections ---------- */

const CORRECTABLE = [
  'address_point_id', 'street_name', 'house_number_cp', 'house_number_co',
  'psc', 'obec_name', 'cast_obce_name', 'okres_name',
] as const;

function InspectorSection({ source }: { source: string }) {
  const [query, setQuery] = useState('');
  const [submitted, setSubmitted] = useState<string | null>(null);
  const inspector = useQuery({
    queryKey: ['location', 'inspector', source, submitted],
    queryFn: () =>
      /^\d+$/.test(submitted!.trim())
        ? fetchInspector(submitted!)
        : fetchInspectorByNative(source, submitted!),
    enabled: submitted != null && submitted.trim() !== '',
    retry: false,
  });
  const ins = inspector.data?.data ?? null;
  return (
    <Card title="Listing inspector (read-your-writes)">
      <form
        className="flex items-end gap-2 max-w-md"
        onSubmit={(e) => { e.preventDefault(); setSubmitted(query); }}
      >
        {/* The submit button stays OUTSIDE the Field: anything inside the
          * <label> would join the input's accessible name and fire label
          * activation. */}
        <Field label="Listing or native id" as="control" className="flex-1">
          <input
            className="w-full rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-2 py-1.5 text-sm"
            placeholder={`listing id, or ${source} native id`}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </Field>
        <button
          type="submit"
          className="rounded-[var(--radius-xs)] border border-[var(--color-copper)] px-3 py-1.5 text-sm text-[var(--color-copper)] hover:bg-[var(--color-copper-soft)]"
        >
          Inspect
        </button>
      </form>
      {inspector.error ? <div className="mt-3"><ErrorBanner error={inspector.error} /></div> : null}
      {ins ? <InspectorBody ins={ins} onCorrected={() => inspector.refetch()} /> : null}
    </Card>
  );
}

function InspectorBody({ ins, onCorrected }: { ins: Inspector; onCorrected: () => void }) {
  const p = ins.projection ?? {};
  const axis = (k: string) => String(p[k] ?? '∅');
  /* The answer table's four read axes. There is no stored display label: the
   * row carries its parts and the reader composes them. */
  const AXES: [string, string][] = [
    ['granularity', 'granularity'], ['match_confidence', 'confidence'],
    ['uncertainty_radius_m', 'radius m'], ['country_status', 'country'],
    ['resolver_version', 'resolver'], ['registry_version', 'registry'],
  ];
  const label = [
    [p.street_name, p.house_number_cp].filter(Boolean).join(' '),
    p.obec_name,
  ].filter(Boolean).join(', ');
  return (
    <div className="mt-4 space-y-4">
      <div>
        <div className="text-sm">
          <span className="font-mono text-[0.8rem]">#{ins.listing_id}</span>{' '}
          <span className="text-[var(--color-ink-2)]">{label || '∅'}</span>
          {p.disputed ? (
            <span className="ml-2 text-[0.7rem] text-[var(--color-brick)]">
              DISPUTED · {String(p.disputed)}
            </span>
          ) : null}
        </div>
        <div className="mt-2 grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
          {AXES.map(([k, label_]) => <Stat key={k} label={label_} value={axis(k)} />)}
        </div>
        <div className="mt-1 text-[0.68rem] text-[var(--color-ink-4)]">
          street {axis('street_name')} · čp {axis('house_number_cp')} · čo {axis('house_number_co')} ·
          obec {axis('obec_name')} · PSČ {axis('psc')} · kód ADM {axis('ruian_adm_kod')} ·
          resolved {axis('resolved_at')}
        </div>
      </div>

      <CorrectionForm listingId={ins.listing_id} onDone={onCorrected} />

      <details>
        <summary className="cursor-pointer text-sm text-[var(--color-ink-2)]">
          Claims ({ins.claims.length})
        </summary>
        <div className="mt-2 overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]">
                <th className="py-1 pr-3 font-medium">Claim</th>
                <th className="py-1 pr-3 font-medium">Value</th>
                <th className="py-1 pr-3 font-medium">Method</th>
                <th className="py-1 pr-3 font-medium">Licence</th>
                <th className="py-1 font-medium">Observed</th>
              </tr>
            </thead>
            <tbody>
              {ins.claims.map((c) => (
                <tr key={c.id} className="border-t border-[var(--color-rule-soft)]">
                  <td className="py-1 pr-3">{c.claim_type}</td>
                  <td className="py-1 pr-3 font-mono text-[0.75rem]">{c.value_text ?? c.value_num ?? '—'}</td>
                  <td className="py-1 pr-3 font-mono text-[0.75rem]">{c.extraction_method}</td>
                  <td className="py-1 pr-3">{c.licence_class}</td>
                  <td className="py-1 text-[var(--color-ink-3)]">{fmtRelative(c.first_observed_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}

function CorrectionForm({ listingId, onDone }: { listingId: number; onDone: () => void }) {
  const [claimType, setClaimType] = useState<string>('street_name');
  const [value, setValue] = useState('');
  const [note, setNote] = useState('');
  const [result, setResult] = useState<CorrectionResult | null>(null);
  const mut = useMutation({
    mutationFn: () =>
      submitCorrection({ listing_id: listingId, claim_type: claimType, value_text: value, note: note || undefined }),
    onSuccess: (res) => { setResult(res.data); setValue(''); onDone(); },
  });
  const input = 'rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-2 py-1.5 text-sm';
  return (
    <div className="rounded-[var(--radius-sm)] border border-[var(--color-rule-soft)] px-3 py-2.5">
      <div className="text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)] mb-2">
        Operator correction (appends a claim, resolves immediately)
      </div>
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => { e.preventDefault(); if (value.trim()) mut.mutate(); }}
      >
        <Field label="Claim type" as="control">
          <select className={input} value={claimType} onChange={(e) => setClaimType(e.target.value)}>
            {CORRECTABLE.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </Field>
        <Field label="Corrected value" as="control" className="flex-1 min-w-40">
          <input
            className={`${input} w-full`}
            placeholder={claimType === 'address_point_id' ? 'kód ADM (digits)' : 'corrected value'}
            value={value} onChange={(e) => setValue(e.target.value)}
          />
        </Field>
        <Field label="Note" as="control" className="min-w-32">
          <input
            className={`${input} w-full`} placeholder="note (optional)"
            value={note} onChange={(e) => setNote(e.target.value)}
          />
        </Field>
        <button
          type="submit" disabled={mut.isPending || !value.trim()}
          className="rounded-[var(--radius-xs)] bg-[var(--color-copper)] px-3 py-1.5 text-sm text-white hover:bg-[var(--color-copper-2)] disabled:opacity-50"
        >
          {mut.isPending ? 'Applying…' : 'Correct'}
        </button>
      </form>
      {mut.error ? <div className="mt-2"><ErrorBanner error={mut.error} /></div> : null}
      {result ? (
        <div className="mt-2 text-[0.75rem] text-[var(--color-ink-2)]">
          {result.restatement ? 'Restated an existing claim' : 'Claim appended'} ·{' '}
          {result.resolved ? 'resolved synchronously' : 'queued for the next drain (≤15 min)'}
          {result.registry_echo ? (
            <span> · registry: {String(result.registry_echo.street ?? '')} {String(result.registry_echo.cislo_domovni ?? '')}
              {result.registry_echo.cislo_orientacni ? `/${result.registry_echo.cislo_orientacni}` : ''},{' '}
              {String(result.registry_echo.obec ?? '')} {String(result.registry_echo.psc ?? '')}</span>
          ) : null}
          {result.projection ? (
            <span> · now: {String(result.projection.granularity)} / {String(result.projection.street_name ?? '∅')}</span>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/* ---------- page ---------- */

export default function LocationQuality() {
  const [source, setSource] = useState<string>('bezrealitky');
  const overview = useQuery({
    queryKey: ['location', 'overview', source],
    queryFn: () => fetchSourceOverview(source),
    staleTime: 60_000,
  });
  const summary = useQuery({
    queryKey: ['location', 'summary'],
    queryFn: fetchCorpusSummary,
    staleTime: 5 * 60_000,
  });
  const ov = overview.data?.data;
  const summaryRows = summary.data?.data.sources ?? [];
  const refreshedAt = overview.dataUpdatedAt
    ? fmtRelative(new Date(overview.dataUpdatedAt).toISOString())
    : null;
  const select = useMemo(
    () => (
      <Field label="Source" as="control">
        <select
          className="rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-2 py-1.5 text-sm"
          value={source}
          onChange={(e) => setSource(e.target.value)}
        >
          {LOCATION_SOURCES.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </Field>
    ),
    [source],
  );

  return (
    <div className="px-6 pt-5 pb-8 max-w-screen-2xl mx-auto">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl leading-tight">Location quality</h1>
          <p className="text-sm text-[var(--color-ink-2)]">
            Read entirely from <span className="font-mono">listing_location</span> (listing grain)
            {refreshedAt ? <> · refreshed {refreshedAt}</> : null}
          </p>
        </div>
        {select}
      </header>

      <div className="mt-4 space-y-4">

        {overview.error ? <ErrorBanner error={overview.error} /> : null}
        {overview.isLoading && !ov ? (
          <div className="animate-pulse space-y-4">
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
              {Array.from({ length: 6 }).map((_, i) => (
                <div key={i} className="h-16 rounded-[var(--radius-sm)] border border-[var(--color-rule-soft)] bg-[var(--color-paper-2)]" />
              ))}
            </div>
            <div className="h-[280px] rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)]" />
          </div>
        ) : ov ? <OverviewPanels ov={ov} /> : null}

        <InspectorSection source={source} />

        <Card title="All sources (active listings, listing grain)">
          {summary.error ? <ErrorBanner error={summary.error} /> : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm max-w-3xl">
                <thead>
                  <tr className="text-left text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]">
                    <th className="py-1.5 pr-3 font-medium">Source</th>
                    <th className="py-1.5 pr-3 font-medium text-right">Active</th>
                    <th className="py-1.5 pr-3 font-medium text-right">With a town</th>
                    <th className="py-1.5 pr-3 font-medium text-right">≥ building</th>
                    <th className="py-1.5 pr-3 font-medium text-right">≥ street</th>
                    <th className="py-1.5 pr-3 font-medium text-right">kód ADM</th>
                    <th className="py-1.5 font-medium text-right">Disputed</th>
                  </tr>
                </thead>
                <tbody>
                  {summaryRows.map((r) => (
                    <tr
                      key={r.source}
                      className={`border-t border-[var(--color-rule-soft)] cursor-pointer hover:bg-[var(--color-copper-soft)] ${r.source === source ? 'bg-[var(--color-copper-soft)]' : ''}`}
                      onClick={() => setSource(r.source)}
                    >
                      <td className="py-1.5 pr-3">{r.source}</td>
                      <td className="py-1.5 pr-3 text-right font-mono tabular-nums">{fmtCount(r.active_rows)}</td>
                      <td className="py-1.5 pr-3 text-right font-mono tabular-nums">
                        {r.active_rows ? ((100 * r.with_obec_kod) / r.active_rows).toFixed(1) : '—'}%
                      </td>
                      <td className="py-1.5 pr-3 text-right font-mono tabular-nums">
                        {r.active_rows ? ((100 * r.building_or_better) / r.active_rows).toFixed(1) : '—'}%
                      </td>
                      <td className="py-1.5 pr-3 text-right font-mono tabular-nums">
                        {r.active_rows ? ((100 * r.street_or_better) / r.active_rows).toFixed(1) : '—'}%
                      </td>
                      <td className="py-1.5 pr-3 text-right font-mono tabular-nums">
                        {r.active_rows ? ((100 * r.with_adm_kod) / r.active_rows).toFixed(1) : '—'}%
                      </td>
                      <td className="py-1.5 text-right font-mono tabular-nums">{fmtCount(r.disputed)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
