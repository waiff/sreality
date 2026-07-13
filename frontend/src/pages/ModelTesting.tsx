import { Fragment, type ReactNode, useEffect, useMemo, useState } from 'react';
import { useQuery, keepPreviousData } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  DANGER_VERDICT,
  LANE_LABEL,
  LANES,
  type BakeoffRow,
  type CheckType,
  type Lane,
  type PairFilter,
  type PairGroup,
  distinctCategories,
  distinctModels,
  fetchBakeoffRows,
  fetchBakeoffRunLabels,
  filterPairs,
  groupPairs,
  summarize,
} from '@/lib/bakeoff';
import { fetchImagesByListingIds } from '@/lib/queries';
import { imageSrc } from '@/lib/imageUrl';
import type { ImagePublic } from '@/lib/types';

/*
 * Vision-model bake-off explorer (Session 3). Reads the per-pair × per-model × per-lane verdict
 * matrix persisted by scripts/validate_vision_models.py --persist-results (migration 303) and lets
 * the operator step through every benchmarked pair to SEE where a candidate model would wrongly
 * merge (emit compare High / floor same_layout / site same_unit on a confirmed-DIFFERENT pair) —
 * the precision failures behind docs/design/dedup-vision-model-bakeoff-2026-07.md.
 */

const pct = (v: number | null): string => (v == null ? '—' : `${(100 * v).toFixed(0)}%`);

/* A cell's colour reflects SAFETY: red = the model emitted a MERGE verdict (High / same_layout /
 * same_unit) on a pair whose ground truth is DIFFERENT (is_same === false) — the actual false-merge.
 * A merge verdict on a same-property pair is CORRECT, not dangerous, so it must NOT be red: on a
 * compare recall pair the expected verdict literally IS "High" (the danger verdict), and reproducing
 * it is the goal. Green = correct; amber = a non-dangerous miss (e.g. different_unit→inconclusive). */
function verdictClasses(row: BakeoffRow | undefined): string {
  if (!row) return 'text-[var(--color-ink-3)]';
  if (row.is_dangerous && row.is_same === false)
    return 'bg-[var(--color-brick-soft)] text-[var(--color-brick)] font-medium';
  if (row.is_correct) return 'bg-[var(--color-sage-soft)] text-[var(--color-sage)]';
  return 'bg-[var(--color-ochre-soft)] text-[var(--color-ochre)]';
}

function StatCell({ pctVal, n }: { pctVal: number | null; n: number }) {
  const tone =
    pctVal == null
      ? 'text-[var(--color-ink-3)]'
      : pctVal >= 0.99
        ? 'text-[var(--color-sage)]'
        : pctVal >= 0.85
          ? 'text-[var(--color-ochre)]'
          : 'text-[var(--color-brick)]';
  return (
    <span className={tone}>
      {pct(pctVal)}
      <span className="text-[var(--color-ink-3)] text-xs"> ({n})</span>
    </span>
  );
}

export default function ModelTesting() {
  const labelsQ = useQuery({ queryKey: ['bakeoff', 'labels'], queryFn: fetchBakeoffRunLabels });
  const [runLabel, setRunLabel] = useState<string | null>(null);
  useEffect(() => {
    if (runLabel == null && labelsQ.data && labelsQ.data.length > 0) setRunLabel(labelsQ.data[0]);
  }, [labelsQ.data, runLabel]);

  const rowsQ = useQuery({
    queryKey: ['bakeoff', 'rows', runLabel],
    queryFn: () => fetchBakeoffRows(runLabel as string),
    enabled: !!runLabel,
    placeholderData: keepPreviousData,
  });

  const rows = useMemo(() => rowsQ.data ?? [], [rowsQ.data]);
  const models = useMemo(() => distinctModels(rows), [rows]);
  const matrix = useMemo(() => summarize(rows), [rows]);
  const allPairs = useMemo(() => groupPairs(rows), [rows]);
  const categories = useMemo(() => distinctCategories(allPairs), [allPairs]);

  const [filter, setFilter] = useState<PairFilter>({
    lane: 'all',
    checkType: 'all',
    category: 'all',
    disagreementsOnly: false,
    dangerousOnly: false,
  });
  const pairs = useMemo(() => filterPairs(allPairs, filter), [allPairs, filter]);

  const [idx, setIdx] = useState(0);
  useEffect(() => setIdx(0), [filter, runLabel]);
  const current = pairs[Math.min(idx, Math.max(0, pairs.length - 1))];

  return (
    <div className="mx-auto max-w-6xl px-4 py-6 text-[var(--color-ink)]">
      <header className="flex flex-wrap items-baseline justify-between gap-3 border-b border-[var(--color-rule)] pb-3">
        <div>
          <h1 className="text-2xl tracking-tight">Model testing</h1>
          <p className="text-sm text-[var(--color-ink-3)] mt-1 max-w-2xl">
            Dedup vision bake-off: every candidate model's verdict on each golden pair, side by side.
            A red cell = the model emitted the <em>dangerous</em> verdict (would wrongly merge /
            fail the guard) on a confirmed-different pair. See{' '}
            <span className="text-[var(--color-ink-2)]">
              docs/design/dedup-vision-model-bakeoff-2026-07.md
            </span>
            .
          </p>
        </div>
        <label className="text-sm text-[var(--color-ink-2)]">
          Run{' '}
          <select
            className="ml-1 rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-inset)] px-2 py-1"
            value={runLabel ?? ''}
            onChange={(e) => setRunLabel(e.target.value)}
          >
            {(labelsQ.data ?? []).map((l) => (
              <option key={l} value={l}>
                {l}
              </option>
            ))}
          </select>
        </label>
      </header>

      {rowsQ.isLoading && <p className="mt-6 text-sm text-[var(--color-ink-3)]">Loading…</p>}
      {!rowsQ.isLoading && rows.length === 0 && (
        <p className="mt-6 text-sm text-[var(--color-ink-3)]">
          No results for this run yet. Dispatch{' '}
          <span className="text-[var(--color-ink-2)]">validate_vision_models.yml</span> with a{' '}
          <span className="text-[var(--color-ink-2)]">run_label</span> to populate it.
        </p>
      )}

      {rows.length > 0 && (
        <>
          {/* summary matrix */}
          <section className="mt-6">
            <h2 className="text-sm uppercase tracking-[0.18em] text-[var(--color-ink-3)] mb-2">
              Recall / precision by model × lane
            </h2>
            <div className="overflow-x-auto">
              <table className="min-w-full text-sm border border-[var(--color-rule)] rounded-[var(--radius-sm)]">
                <thead>
                  <tr className="bg-[var(--color-inset)] text-left">
                    <th className="px-3 py-2 font-medium">Model</th>
                    {LANES.map((lane) => (
                      <th key={lane} className="px-3 py-2 font-medium" colSpan={2}>
                        {LANE_LABEL[lane]}
                      </th>
                    ))}
                  </tr>
                  <tr className="bg-[var(--color-inset)] text-left text-xs text-[var(--color-ink-3)]">
                    <th className="px-3 py-1" />
                    {LANES.map((lane) => (
                      <Fragment key={lane}>
                        <th className="px-3 py-1 font-normal">recall</th>
                        <th className="px-3 py-1 font-normal">precision</th>
                      </Fragment>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {models.map((model) => {
                    const m = matrix.get(model);
                    return (
                      <tr key={model} className="border-t border-[var(--color-rule)]">
                        <td className="px-3 py-2 font-mono text-xs">{model}</td>
                        {LANES.map((lane) => (
                          <Fragment key={lane}>
                            <td className="px-3 py-2">
                              <StatCell pctVal={m?.[lane].recall.pct ?? null} n={m?.[lane].recall.n ?? 0} />
                            </td>
                            <td className="px-3 py-2">
                              <StatCell
                                pctVal={m?.[lane].precision.pct ?? null}
                                n={m?.[lane].precision.n ?? 0}
                              />
                            </td>
                          </Fragment>
                        ))}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <p className="text-xs text-[var(--color-ink-3)] mt-1">
              precision = share of confirmed-different pairs the model did NOT wrongly merge (higher
              = safer). Green ≥ 99%, amber ≥ 85%, red below. Read recall relative to the Sonnet
              baseline row (forensic verdicts are ~5% non-deterministic).
            </p>
          </section>

          {/* filters */}
          <section className="mt-6 flex flex-wrap items-center gap-3 text-sm">
            <Select
              label="Lane"
              value={filter.lane}
              onChange={(v) => setFilter((f) => ({ ...f, lane: v as Lane | 'all' }))}
              options={['all', ...LANES]}
            />
            <Select
              label="Check"
              value={filter.checkType}
              onChange={(v) => setFilter((f) => ({ ...f, checkType: v as CheckType | 'all' }))}
              options={['all', 'recall', 'precision']}
            />
            <Select
              label="Category"
              value={filter.category}
              onChange={(v) => setFilter((f) => ({ ...f, category: v }))}
              options={['all', ...categories]}
            />
            <Checkbox
              label="Dangerous only"
              checked={filter.dangerousOnly}
              onChange={(c) => setFilter((f) => ({ ...f, dangerousOnly: c }))}
            />
            <Checkbox
              label="Disagreements only"
              checked={filter.disagreementsOnly}
              onChange={(c) => setFilter((f) => ({ ...f, disagreementsOnly: c }))}
            />
          </section>

          {/* pair navigator + detail */}
          <section className="mt-4">
            <div className="flex items-center justify-between gap-3 text-sm">
              <div className="text-[var(--color-ink-3)]">
                {pairs.length === 0 ? 'No pairs match' : `Pair ${Math.min(idx + 1, pairs.length)} of ${pairs.length}`}
              </div>
              <div className="flex gap-2">
                <button
                  className="rounded-[var(--radius-xs)] border border-[var(--color-rule)] px-3 py-1 disabled:opacity-40"
                  onClick={() => setIdx((i) => Math.max(0, i - 1))}
                  disabled={idx <= 0}
                >
                  ← Prev
                </button>
                <button
                  className="rounded-[var(--radius-xs)] border border-[var(--color-rule)] px-3 py-1 disabled:opacity-40"
                  onClick={() => setIdx((i) => Math.min(pairs.length - 1, i + 1))}
                  disabled={idx >= pairs.length - 1}
                >
                  Next →
                </button>
              </div>
            </div>

            {current && <PairDetail pair={current} models={models} />}
          </section>
        </>
      )}
    </div>
  );
}

function PairDetail({ pair, models }: { pair: PairGroup; models: string[] }) {
  const imagesQ = useQuery({
    queryKey: ['bakeoff', 'images', pair.a, pair.b],
    queryFn: () => fetchImagesByListingIds([pair.a, pair.b], 6),
    placeholderData: keepPreviousData,
  });
  const imgs: Map<number, ImagePublic[]> = imagesQ.data ?? new Map();
  const lanesForPair = LANES.filter((lane) =>
    [...pair.byModelLane.keys()].some((k) => k.endsWith(`|${lane}`)),
  );

  return (
    <div className="mt-3 rounded-[var(--radius-sm)] border border-[var(--color-rule)] p-4">
      {/* ground truth */}
      <div className="flex flex-wrap items-center gap-2 text-xs mb-3">
        <Chip>
          {pair.check_type === 'precision' ? 'confirmed DIFFERENT' : 'recall reference'}
        </Chip>
        {pair.category_main && <Chip>{pair.category_main}</Chip>}
        {pair.label_source && <Chip>{pair.label_source}</Chip>}
        <Chip>
          A {pair.a} · B {pair.b}
        </Chip>
        {pair.anyDangerous && (
          <span className="text-[var(--color-brick)] font-medium">⚠ a model would merge these</span>
        )}
      </div>

      {/* images side by side */}
      <div className="grid grid-cols-2 gap-4">
        {[pair.a, pair.b].map((sid, side) => (
          <div key={sid}>
            <div className="text-xs text-[var(--color-ink-3)] mb-1">
              Listing {side === 0 ? 'A' : 'B'} ·{' '}
              <Link className="underline" to={`/listing/${sid}`} target="_blank">
                {sid}
              </Link>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {(imgs.get(sid) ?? []).map((im) => (
                <img
                  key={im.id}
                  src={imageSrc({ sreality_url: im.sreality_url ?? '', storage_path: im.storage_path })}
                  alt=""
                  loading="lazy"
                  className="h-24 w-24 object-cover rounded-[var(--radius-xs)] border border-[var(--color-rule)]"
                />
              ))}
              {(imgs.get(sid) ?? []).length === 0 && (
                <span className="text-xs text-[var(--color-ink-3)]">no images</span>
              )}
            </div>
          </div>
        ))}
      </div>

      {/* verdict grid: models × lanes evaluated for this pair */}
      <div className="overflow-x-auto mt-4">
        <table className="min-w-full text-sm border border-[var(--color-rule)] rounded-[var(--radius-sm)]">
          <thead>
            <tr className="bg-[var(--color-inset)] text-left text-xs">
              <th className="px-3 py-2 font-medium">Model</th>
              {lanesForPair.map((lane) => (
                <th key={lane} className="px-3 py-2 font-medium">
                  {LANE_LABEL[lane]}{' '}
                  <span className="text-[var(--color-ink-3)] font-normal">
                    (danger: {DANGER_VERDICT[lane]})
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {models.map((model) => (
              <tr key={model} className="border-t border-[var(--color-rule)]">
                <td className="px-3 py-2 font-mono text-xs">{model}</td>
                {lanesForPair.map((lane) => {
                  const row = pair.byModelLane.get(`${model}|${lane}`);
                  return (
                    <td key={lane} className="px-3 py-2">
                      {row ? (
                        <span className={`rounded-[var(--radius-xs)] px-1.5 py-0.5 ${verdictClasses(row)}`}>
                          {row.candidate_verdict}
                          {row.room_type && lane === 'compare' ? (
                            <span className="text-[var(--color-ink-3)] text-xs"> · {row.room_type}</span>
                          ) : null}
                        </span>
                      ) : (
                        <span className="text-[var(--color-ink-3)]">—</span>
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Chip({ children }: { children: ReactNode }) {
  return (
    <span className="rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-inset)] px-1.5 py-0.5">
      {children}
    </span>
  );
}

function Select({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: readonly string[];
}) {
  return (
    <label className="text-[var(--color-ink-2)]">
      {label}{' '}
      <select
        className="rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-inset)] px-2 py-1"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </label>
  );
}

function Checkbox({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (c: boolean) => void;
}) {
  return (
    <label className="flex items-center gap-1.5 text-[var(--color-ink-2)] cursor-pointer">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      {label}
    </label>
  );
}
