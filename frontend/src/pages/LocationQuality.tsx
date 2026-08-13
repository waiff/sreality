/* Location quality — the FIRST consumer of the location serving projection
 * (location program W1v; design 05 §5.5.4). Admin-only; every read goes
 * through the identity-gated `/location/*` API (the location tables are
 * service-role-only). Every precision panel states its grain — mixing
 * listing- and property-grain precision is a lie by aggregation (§5.5.4).
 *
 * Visual notes: enum mixes are single-hue horizontal bars (magnitude of one
 * measure, so one hue — identity color would be noise); pin-collision classes
 * render as a labeled status list (dot + text, never color alone) because the
 * muted 4-hue token set cannot carry a 6-slot categorical stack (validated,
 * it fails CVD + normal-vision floors). The full per-cluster detail is the
 * table below it. */

import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  fetchCorpusSummary, fetchInspector, fetchInspectorByNative, fetchSample,
  fetchSampleScore, fetchSourceOverview, fetchW1vGate, saveMemberLabels,
  submitCorrection,
  GRANULARITY_VALUES, LOCATION_SOURCES,
  type CorrectionResult, type Inspector, type MixRow, type SampleMember,
  type ScoreBlock, type SourceOverview,
} from '../lib/locationQuality';
import { ApiError } from '../lib/api';
import { fmtCount, fmtRelative } from '../lib/format';

/* ---------- shared bits (Costs.tsx idiom) ---------- */

function Card({ title, accessory, children }: {
  title: string; accessory?: React.ReactNode; children: React.ReactNode;
}) {
  return (
    <section className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
          {title}
        </h3>
        {accessory ?? null}
      </div>
      <div className="mt-3">{children}</div>
    </section>
  );
}

function Stat({ label, value, hint, accent, danger }: {
  label: string; value: string; hint?: string; accent?: boolean; danger?: boolean;
}) {
  const tone = danger ? 'text-[var(--color-brick)]'
    : accent ? 'text-[var(--color-copper-2)]' : 'text-[var(--color-ink)]';
  return (
    <div className="rounded-[var(--radius-sm)] border border-[var(--color-rule-soft)] bg-[var(--color-paper-2)] px-3 py-2">
      <div className="text-[0.62rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]">{label}</div>
      <div className={`font-mono tabular-nums text-xl ${tone}`}>{value}</div>
      {hint ? <div className="text-[0.68rem] text-[var(--color-ink-4)]">{hint}</div> : null}
    </div>
  );
}

function PassPill({ pass, label }: { pass: boolean | null | undefined; label: string }) {
  const cls = pass == null
    ? 'bg-[var(--color-paper-2)] text-[var(--color-ink-3)] border-[var(--color-rule)]'
    : pass
      ? 'bg-[var(--color-sage-soft)] text-[var(--color-sage)] border-transparent'
      : 'bg-[var(--color-brick-soft)] text-[var(--color-brick)] border-transparent';
  return (
    <span className={`inline-block rounded-full border px-2 py-0.5 text-[0.65rem] font-medium ${cls}`}>
      {label} {pass == null ? '—' : pass ? 'PASS' : 'FAIL'}
    </span>
  );
}

/* Single-hue horizontal bar list: magnitude across enum values. */
function BarList({ rows, total }: { rows: MixRow[]; total: number }) {
  if (!rows.length) return <p className="text-sm text-[var(--color-ink-3)]">No rows.</p>;
  return (
    <div className="space-y-1.5">
      {rows.map((r) => {
        const pct = total ? (100 * r.n) / total : 0;
        return (
          <div key={r.value ?? 'null'} className="grid grid-cols-[10rem_1fr_5.5rem] items-center gap-2 text-sm">
            <span className="truncate text-[var(--color-ink-2)]" title={r.value ?? '∅'}>
              {r.value ?? '∅'}
            </span>
            <span className="h-3 rounded-[2px] bg-[var(--color-rule-soft)] overflow-hidden">
              <span
                className="block h-full rounded-[2px] bg-[var(--color-copper)]"
                style={{ width: `${Math.max(pct, 0.5)}%` }}
              />
            </span>
            <span className="text-right font-mono tabular-nums text-[0.78rem]">
              {fmtCount(r.n)} <span className="text-[var(--color-ink-4)]">{pct.toFixed(1)}%</span>
            </span>
          </div>
        );
      })}
    </div>
  );
}

const CLASS_TONE: Record<string, string> = {
  normal: 'var(--color-sage)',
  legitimate_multiunit: 'var(--color-copper)',
  building_1_to_many: 'var(--color-copper)',
  town_centroid_suspect: 'var(--color-ochre)',
  parser_collapse_suspect: 'var(--color-brick)',
  foreign_resort_centroid: 'var(--color-ink-3)',
};

function ClassDot({ cls }: { cls: string }) {
  return (
    <span
      className="inline-block size-2 rounded-full align-middle"
      style={{ background: CLASS_TONE[cls] ?? 'var(--color-ink-4)' }}
    />
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

/* ---------- gate card ---------- */

function GateCard() {
  const { data, error } = useQuery({
    queryKey: ['location', 'w1v-gate'],
    queryFn: fetchW1vGate,
    staleTime: 60_000,
    refetchInterval: 5 * 60_000,
  });
  if (error) return <Card title="W1v gate — bezrealitky"><ErrorBanner error={error} /></Card>;
  const g = data?.data;
  if (!g) return null;
  return (
    <Card
      title="W1v gate — bezrealitky (listing grain)"
      accessory={
        <div className="flex gap-2">
          <PassPill pass={g.primary_pass} label="primary ≥95%" />
          <PassPill pass={g.fallback_pass} label="fallback ≥90%" />
        </div>
      }
    >
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
        <Stat label="Active rows" value={fmtCount(g.active_rows)} />
        <Stat label="ruianId claim" value={fmtCount(g.with_ruian_claim)} />
        <Stat
          label="→ exactly 1 point" value={fmtCount(g.claim_matches_one_point)}
          hint={g.primary_pct != null ? `${g.primary_pct}% of active` : undefined}
          accent={g.primary_pass} danger={g.primary_pct != null && !g.primary_pass}
        />
        <Stat label="Projection R0" value={fmtCount(g.projection_r0)} hint="match_confidence = exact" />
        <Stat
          label="≥ building" value={fmtCount(g.projection_building_or_better)}
          hint={g.fallback_pct != null ? `${g.fallback_pct}% of active` : undefined}
        />
      </div>
    </Card>
  );
}

/* ---------- source overview panels ---------- */

function OverviewPanels({ ov }: { ov: SourceOverview }) {
  const t = ov.totals;
  const pct = (n: number) => (t.active_rows ? `${((100 * n) / t.active_rows).toFixed(1)}%` : '—');
  const classMix = ov.mixes.pin_collision_class ?? [];
  return (
    <>
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
        <Stat label="Active rows" value={fmtCount(t.active_rows)} />
        <Stat label="≥ building" value={pct(t.building_or_better)} hint={`${fmtCount(t.building_or_better)} rows`} accent />
        <Stat label="≥ street" value={pct(t.street_or_better)} hint={`${fmtCount(t.street_or_better)} rows`} />
        <Stat label="Geo-blockable" value={pct(t.geo_blockable)} hint={`${fmtCount(t.geo_blockable)} rows`} />
        <Stat label="kód ADM" value={pct(t.with_adm_kod)} hint={`${fmtCount(t.with_adm_kod)} rows`} />
        <Stat label="Disputed" value={fmtCount(t.disputed)} danger={t.disputed > 0} />
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <Card title="Granularity × count (listing grain)">
          <BarList rows={ov.mixes.granularity ?? []} total={t.active_rows} />
        </Card>
        <Card title="Position source">
          <BarList rows={ov.mixes.position_source ?? []} total={t.active_rows} />
        </Card>
        <Card title="Admin assignment method">
          <BarList rows={ov.mixes.admin_assignment_method ?? []} total={t.active_rows} />
        </Card>
        <Card title="Match confidence">
          <BarList rows={ov.mixes.match_confidence ?? []} total={t.active_rows} />
        </Card>
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <Card title="Pin sharing (listings per shared pin)">
          <BarList
            rows={pinBuckets(ov)}
            total={t.active_rows}
          />
          <div className="mt-3 space-y-1 text-sm">
            {classMix.map((r) => (
              <div key={r.value ?? 'null'} className="flex items-center gap-2">
                <ClassDot cls={r.value ?? ''} />
                <span className="text-[var(--color-ink-2)]">{r.value ?? '∅'}</span>
                <span className="ml-auto font-mono tabular-nums text-[0.78rem]">{fmtCount(r.n)}</span>
              </div>
            ))}
          </div>
        </Card>
        <Card title="Top shared-pin clusters (current epoch)">
          {ov.top_clusters.length === 0 ? (
            <p className="text-sm text-[var(--color-ink-3)]">No clusters for this source.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]">
                    <th className="py-1.5 pr-3 font-medium">Near</th>
                    <th className="py-1.5 pr-3 font-medium text-right">Listings</th>
                    <th className="py-1.5 pr-3 font-medium text-right">Streets</th>
                    <th className="py-1.5 font-medium">Class</th>
                  </tr>
                </thead>
                <tbody>
                  {ov.top_clusters.map((c) => (
                    <tr key={c.cell_key} className="border-t border-[var(--color-rule-soft)]">
                      <td className="py-1.5 pr-3">{c.nearest_admin_unit ?? c.cell_key}</td>
                      <td className="py-1.5 pr-3 text-right font-mono tabular-nums">{c.listing_count}</td>
                      <td className="py-1.5 pr-3 text-right font-mono tabular-nums">{c.distinct_streets}</td>
                      <td className="py-1.5">
                        <ClassDot cls={c.classification} />{' '}
                        <span className="text-[var(--color-ink-2)]">{c.classification}</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
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
          <BarList rows={ov.mixes.registry_version ?? []} total={t.active_rows} />
          <p className="mt-2 text-[0.7rem] text-[var(--color-ink-4)]">
            Rows on an older registry version re-resolve on the next drain after a registry load —
            a growing stale share means the drain is behind, not that data got worse.
          </p>
        </Card>
      </div>
    </>
  );
}

function pinBuckets(ov: SourceOverview): MixRow[] {
  const order = ['1', '2', '3-4', '5-9', '10-19', '20-49', '50+'];
  const byBucket = new Map<string, number>();
  for (const row of ov.pin_histogram) {
    byBucket.set(row.bucket, (byBucket.get(row.bucket) ?? 0) + row.n);
  }
  return order.filter((b) => byBucket.has(b)).map((b) => ({ value: b, n: byBucket.get(b)! }));
}

/* ---------- labelled sample ---------- */

function ScoreRow({ name, block }: { name: string; block: ScoreBlock }) {
  const fmtPct = (v: number | null | undefined) => (v == null ? '—' : `${v.toFixed(1)}%`);
  return (
    <tr className="border-t border-[var(--color-rule-soft)]">
      <td className="py-1.5 pr-3">{name}</td>
      <td className="py-1.5 pr-3 text-right font-mono tabular-nums">{block.determinable}</td>
      <td className="py-1.5 pr-3 text-right font-mono tabular-nums">
        {fmtPct(block.new.precision_pct)}
        <span className="text-[var(--color-ink-4)]"> ({block.new.matches}/{block.new.asserted})</span>
      </td>
      <td className="py-1.5 pr-3 text-right font-mono tabular-nums text-[var(--color-ink-2)]">
        {block.old ? fmtPct(block.old.precision_pct) : '—'}
      </td>
      <td className="py-1.5">
        <PassPill pass={block.new.asserted ? block.new.floor_pass : null} label={`≥${block.floor_pct}%`} />
      </td>
    </tr>
  );
}

function MemberEditor({ source, member }: { source: string; member: SampleMember }) {
  const qc = useQueryClient();
  const [form, setForm] = useState({
    label_street: member.label_street ?? '',
    label_street_nd: member.label_street_nd,
    label_house_number: member.label_house_number ?? '',
    label_house_number_nd: member.label_house_number_nd,
    label_obec: member.label_obec ?? '',
    label_obec_nd: member.label_obec_nd,
    label_okres: member.label_okres ?? '',
    label_okres_nd: member.label_okres_nd,
    label_precision_class: member.label_precision_class ?? '',
    label_precision_nd: member.label_precision_nd,
    label_note: member.label_note ?? '',
  });
  const save = useMutation({
    mutationFn: () =>
      saveMemberLabels(source, member.listing_id, {
        ...form,
        label_street: form.label_street || null,
        label_house_number: form.label_house_number || null,
        label_obec: form.label_obec || null,
        label_okres: form.label_okres || null,
        label_precision_class: form.label_precision_class || null,
        label_note: form.label_note || null,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['location', 'sample', source] });
      qc.invalidateQueries({ queryKey: ['location', 'sample-score', source] });
    },
  });
  const input = 'w-full rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-2 py-1 text-sm';
  const nd = (key: keyof typeof form, label: string) => (
    <label className="flex items-center gap-1 text-[0.65rem] text-[var(--color-ink-3)] whitespace-nowrap">
      <input
        type="checkbox"
        checked={form[key] as boolean}
        onChange={(e) => setForm((f) => ({ ...f, [key]: e.target.checked }))}
      />
      {label}
    </label>
  );
  return (
    <tr className="border-t border-[var(--color-rule-soft)] align-top">
      <td className="py-2 pr-3 font-mono text-[0.75rem] whitespace-nowrap">
        #{member.position}{' '}
        {member.source_url ? (
          <a
            className="text-[var(--color-copper)] underline decoration-dotted"
            href={member.source_url} target="_blank" rel="noreferrer"
          >
            {member.source_id_native}
          </a>
        ) : member.source_id_native}
        {member.is_active === false ? (
          <div className="text-[0.65rem] text-[var(--color-ochre)]">delisted</div>
        ) : null}
        {member.labelled_at ? (
          <div className="text-[0.65rem] text-[var(--color-sage)]">labelled</div>
        ) : null}
      </td>
      <td className="py-2 pr-2 min-w-36">
        <input className={input} placeholder="street" value={form.label_street}
          onChange={(e) => setForm((f) => ({ ...f, label_street: e.target.value }))} />
        {nd('label_street_nd', 'not determinable')}
      </td>
      <td className="py-2 pr-2 min-w-20">
        <input className={input} placeholder="č." value={form.label_house_number}
          onChange={(e) => setForm((f) => ({ ...f, label_house_number: e.target.value }))} />
        {nd('label_house_number_nd', 'n/d')}
      </td>
      <td className="py-2 pr-2 min-w-28">
        <input className={input} placeholder="obec" value={form.label_obec}
          onChange={(e) => setForm((f) => ({ ...f, label_obec: e.target.value }))} />
        {nd('label_obec_nd', 'n/d')}
      </td>
      <td className="py-2 pr-2 min-w-28">
        <input className={input} placeholder="okres" value={form.label_okres}
          onChange={(e) => setForm((f) => ({ ...f, label_okres: e.target.value }))} />
        {nd('label_okres_nd', 'n/d')}
      </td>
      <td className="py-2 pr-2">
        <select className={input} value={form.label_precision_class}
          onChange={(e) => setForm((f) => ({ ...f, label_precision_class: e.target.value }))}>
          <option value="">precision…</option>
          {GRANULARITY_VALUES.map((g) => <option key={g} value={g}>{g}</option>)}
        </select>
        {nd('label_precision_nd', 'n/d')}
      </td>
      <td className="py-2 pr-2 min-w-32">
        <input className={input} placeholder="note" value={form.label_note}
          onChange={(e) => setForm((f) => ({ ...f, label_note: e.target.value }))} />
      </td>
      <td className="py-2 text-right">
        <button
          className="rounded-[var(--radius-xs)] border border-[var(--color-copper)] px-2.5 py-1 text-sm text-[var(--color-copper)] hover:bg-[var(--color-copper-soft)] disabled:opacity-50"
          disabled={save.isPending}
          onClick={() => save.mutate()}
        >
          {save.isPending ? 'Saving…' : 'Save'}
        </button>
        {save.error ? <div className="mt-1 text-[0.65rem] text-[var(--color-brick)]">{errText(save.error)}</div> : null}
      </td>
    </tr>
  );
}

function SampleSection({ source }: { source: string }) {
  const [unlabelledOnly, setUnlabelledOnly] = useState(true);
  const sample = useQuery({
    queryKey: ['location', 'sample', source, unlabelledOnly],
    queryFn: () => fetchSample(source, unlabelledOnly),
    staleTime: 30_000,
  });
  const score = useQuery({
    queryKey: ['location', 'sample-score', source],
    queryFn: () => fetchSampleScore(source),
    staleTime: 30_000,
  });

  if (sample.error) return <Card title="Frozen labelled sample"><ErrorBanner error={sample.error} /></Card>;
  const s = sample.data?.data;
  if (!s) return null;
  if (!s.sample) {
    return (
      <Card title="Frozen labelled sample">
        <p className="text-sm text-[var(--color-ink-3)]">
          No sample drawn for {source} yet — dispatch the <span className="font-mono">location_labelled_sample</span>{' '}
          workflow (<span className="font-mono">write=true</span>) to draw it. It must be drawn{' '}
          <em>before</em> that portal's extraction sweep (06 §6.4.0).
        </p>
      </Card>
    );
  }
  const sc = score.data?.data;
  return (
    <Card
      title={`Frozen labelled sample — ${s.sample.labelled}/${s.sample.members} labelled`}
      accessory={
        <label className="flex items-center gap-1.5 text-[0.7rem] text-[var(--color-ink-3)]">
          <input type="checkbox" checked={unlabelledOnly}
            onChange={(e) => setUnlabelledOnly(e.target.checked)} />
          unlabelled only
        </label>
      }
    >
      <p className="text-[0.7rem] text-[var(--color-ink-4)] mb-3">
        Drawn {fmtRelative(s.sample.drawn_at)} · {s.sample.method} · label against the{' '}
        <em>portal page</em> (open the id link) — the system's own answers are deliberately not
        shown here, so the label can't anchor on them.
      </p>
      {sc && sc.labelled > 0 ? (
        <div className="mb-4 overflow-x-auto">
          <table className="w-full text-sm max-w-2xl">
            <thead>
              <tr className="text-left text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]">
                <th className="py-1.5 pr-3 font-medium">Field</th>
                <th className="py-1.5 pr-3 font-medium text-right">Determinable</th>
                <th className="py-1.5 pr-3 font-medium text-right">New system</th>
                <th className="py-1.5 pr-3 font-medium text-right">Old system</th>
                <th className="py-1.5 font-medium">Floor</th>
              </tr>
            </thead>
            <tbody>
              <ScoreRow name="street" block={sc.street} />
              <ScoreRow name="obec" block={sc.obec} />
              <ScoreRow name="okres" block={sc.okres} />
              <ScoreRow name="precision class" block={sc.precision_class} />
            </tbody>
          </table>
        </div>
      ) : null}
      <div className="overflow-x-auto max-h-[560px] overflow-y-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]">
              <th className="py-1.5 pr-3 font-medium">Listing</th>
              <th className="py-1.5 pr-2 font-medium">Street</th>
              <th className="py-1.5 pr-2 font-medium">No.</th>
              <th className="py-1.5 pr-2 font-medium">Obec</th>
              <th className="py-1.5 pr-2 font-medium">Okres</th>
              <th className="py-1.5 pr-2 font-medium">Precision</th>
              <th className="py-1.5 pr-2 font-medium">Note</th>
              <th className="py-1.5 font-medium" />
            </tr>
          </thead>
          <tbody>
            {s.members.map((m) => (
              <MemberEditor key={m.listing_id} source={source} member={m} />
            ))}
          </tbody>
        </table>
      </div>
    </Card>
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
        className="flex gap-2 max-w-md"
        onSubmit={(e) => { e.preventDefault(); setSubmitted(query); }}
      >
        <input
          className="flex-1 rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-2 py-1.5 text-sm"
          placeholder={`listing id, or ${source} native id`}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
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
  const AXES: [string, string][] = [
    ['granularity', 'granularity'], ['position_source', 'position source'],
    ['match_confidence', 'confidence'], ['uncertainty_radius_m', 'radius m'],
    ['admin_assignment_method', 'admin via'], ['position_licence_class', 'licence'],
  ];
  return (
    <div className="mt-4 space-y-4">
      <div>
        <div className="text-sm">
          <span className="font-mono text-[0.8rem]">#{ins.listing_id}</span>{' '}
          <span className="text-[var(--color-ink-2)]">{axis('display_label')}</span>
          {p.location_disputed ? (
            <span className="ml-2 text-[0.7rem] text-[var(--color-brick)]">DISPUTED</span>
          ) : null}
        </div>
        <div className="mt-2 grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
          {AXES.map(([k, label]) => <Stat key={k} label={label} value={axis(k)} />)}
        </div>
        <div className="mt-1 text-[0.68rem] text-[var(--color-ink-4)]">
          street {axis('street_name')} · čp {axis('house_number_cp')} · čo {axis('house_number_co')} ·
          obec {axis('obec_name')} · PSČ {axis('psc')} · kód ADM {axis('ruian_adm_kod')} ·
          built {axis('built_at')}
        </div>
      </div>

      <CorrectionForm listingId={ins.listing_id} onDone={onCorrected} />

      <details>
        <summary className="cursor-pointer text-sm text-[var(--color-ink-2)]">
          Claims ({ins.claims.length}) · candidates ({ins.candidates.length})
        </summary>
        <div className="mt-2 overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]">
                <th className="py-1 pr-3 font-medium">Claim</th>
                <th className="py-1 pr-3 font-medium">Value</th>
                <th className="py-1 pr-3 font-medium">Extractor</th>
                <th className="py-1 pr-3 font-medium">Licence</th>
                <th className="py-1 font-medium">Observed</th>
              </tr>
            </thead>
            <tbody>
              {ins.claims.map((c) => (
                <tr key={c.id} className="border-t border-[var(--color-rule-soft)]">
                  <td className="py-1 pr-3">{c.claim_type}</td>
                  <td className="py-1 pr-3 font-mono text-[0.75rem]">{c.value_text ?? c.value_num ?? '—'}</td>
                  <td className="py-1 pr-3 font-mono text-[0.75rem]">{c.extractor_id}</td>
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
        className="flex flex-wrap gap-2"
        onSubmit={(e) => { e.preventDefault(); if (value.trim()) mut.mutate(); }}
      >
        <select className={input} value={claimType} onChange={(e) => setClaimType(e.target.value)}>
          {CORRECTABLE.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <input
          className={`${input} flex-1 min-w-40`}
          placeholder={claimType === 'address_point_id' ? 'kód ADM (digits)' : 'corrected value'}
          value={value} onChange={(e) => setValue(e.target.value)}
        />
        <input
          className={`${input} min-w-32`} placeholder="note (optional)"
          value={note} onChange={(e) => setNote(e.target.value)}
        />
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
      <select
        className="rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-2 py-1.5 text-sm"
        value={source}
        onChange={(e) => setSource(e.target.value)}
      >
        {LOCATION_SOURCES.map((s) => <option key={s} value={s}>{s}</option>)}
      </select>
    ),
    [source],
  );

  return (
    <div className="px-6 pt-5 pb-8 max-w-screen-2xl mx-auto">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl leading-tight">Location quality</h1>
          <p className="text-sm text-[var(--color-ink-2)]">
            Read entirely from the serving projection (listing grain)
            {refreshedAt ? <> · refreshed {refreshedAt}</> : null}
          </p>
        </div>
        {select}
      </header>

      <div className="mt-4 space-y-4">
        {source === 'bezrealitky' ? <GateCard /> : null}

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

        <SampleSection source={source} />
        <InspectorSection source={source} />

        <Card title="All sources (active listings, listing grain)">
          {summary.error ? <ErrorBanner error={summary.error} /> : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm max-w-3xl">
                <thead>
                  <tr className="text-left text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]">
                    <th className="py-1.5 pr-3 font-medium">Source</th>
                    <th className="py-1.5 pr-3 font-medium text-right">Active</th>
                    <th className="py-1.5 pr-3 font-medium text-right">≥ building</th>
                    <th className="py-1.5 pr-3 font-medium text-right">≥ street</th>
                    <th className="py-1.5 pr-3 font-medium text-right">Geo-blockable</th>
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
                        {r.active_rows ? ((100 * r.building_or_better) / r.active_rows).toFixed(1) : '—'}%
                      </td>
                      <td className="py-1.5 pr-3 text-right font-mono tabular-nums">
                        {r.active_rows ? ((100 * r.street_or_better) / r.active_rows).toFixed(1) : '—'}%
                      </td>
                      <td className="py-1.5 pr-3 text-right font-mono tabular-nums">
                        {r.active_rows ? ((100 * r.geo_blockable) / r.active_rows).toFixed(1) : '—'}%
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
