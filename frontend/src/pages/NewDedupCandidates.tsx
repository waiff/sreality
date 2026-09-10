/* NEW DEDUP · Candidates — the Wave 2 audit page
 * (docs/design/new-dedup/PROGRAM.md, W2 and the ledger of 2026-09-10 (a)/(b)).
 *
 * WHAT THIS PAGE IS FOR. Gate 2, in the operator's words, is: reading this page,
 * am I satisfied that the built path loses no rightful candidate to data quality?
 * So the page is not a status board — it is an argument, in five parts, that the
 * operator either accepts or refuses:
 *   1. the FUNNEL      — how many listings survive each narrowing, and where the
 *                        rest fall out;
 *   2. the MATRIX      — which property types and which paths produced the pairs
 *                        (path A's and path B's columns are here from day one and
 *                        empty, because an omitted column hides the gap);
 *   3. MISSING DATA    — the same losses again, but named as what is absent,
 *                        overall and then portal by portal;
 *   4. TOWN STATISTICS — path C blocks on the town, so the town is where a
 *                        "candidate storm" would show up first;
 *   5. the PARAMETER SET — under which knobs all of the above was computed.
 *
 * IT NEVER COMPUTES A NUMBER THE LANE DID NOT MEASURE. Every figure is read off
 * the generation row's `stats` (written once by
 * scripts/dedup_candidates_generate.py:generation_stats); this page only sums,
 * sorts and takes differences of those counts. Where a number does not exist —
 * an un-migrated store, a run that failed before the statistics step, a path
 * nobody has built — it renders an em dash or says "not yet", never a zero and
 * never a plausible-looking guess.
 *
 * THE RUN IS IN THE URL (`?generation_id=`). Two parameter sets are compared by
 * opening the page twice, so the comparison is a link the operator can keep.
 * Without the parameter the backend shows the newest SUCCESSFUL run.
 */

import { useMemo, useState, type ReactNode } from 'react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';

import {
  getNewDedupCandidateOverview,
  type NewDedupCandidateFunnelRow,
  type NewDedupCandidateGeneration,
  type NewDedupCandidateMatrixRow,
  type NewDedupCandidatePath,
  type NewDedupCandidateRecentGeneration,
  type NewDedupCandidateStats,
} from '@/lib/api';
import CandidateFunnel, { ProportionBar } from '@/components/new-dedup/CandidateFunnel';
import ErrorBanner from '@/components/ErrorBanner';
import Spinner from '@/components/Spinner';
import { categoryMainLabel, categoryTypeLabel } from '@/lib/enums';
import { fmtAbsolute, fmtCount, fmtPct } from '@/lib/format';

/* ------------------------------------------------------------------ chrome */

const TH = 'py-1.5 pr-3 font-medium whitespace-nowrap';
const HEAD = 'text-left text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]';
const TD = 'py-1.5 pr-3';
const NUM = 'py-1.5 pr-3 text-right font-mono tabular-nums';
const ROW = 'border-t border-[var(--color-rule-soft)]';

function Card({
  title,
  lede,
  children,
}: {
  title: string;
  lede: string;
  children: ReactNode;
}) {
  return (
    <section className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4">
      <h2 className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
        {title}
      </h2>
      {/* Every section explains itself in one paragraph before it shows a
        * single number — the operator reads this page to make a decision, not
        * to learn the program's vocabulary. */}
      <p className="mt-2 text-[0.78rem] leading-relaxed text-[var(--color-ink-2)] max-w-[52rem]">
        {lede}
      </p>
      <div className="mt-3">{children}</div>
    </section>
  );
}

function Notice({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="mt-5 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4">
      <p className="text-sm font-medium text-[var(--color-ink)]">{title}</p>
      <div className="mt-1.5 text-[0.8rem] leading-relaxed text-[var(--color-ink-2)] max-w-[52rem]">
        {children}
      </div>
    </div>
  );
}

const dash = (v: string | null | undefined): string => (v == null || v === '' ? '—' : v);
const typeLabel = (cm: string | null | undefined): string =>
  cm == null ? '—' : categoryMainLabel(cm);
const dealLabel = (ct: string | null | undefined): string => categoryTypeLabel(ct);

const share = (part: number | null, whole: number): number | null =>
  part == null || whole <= 0 ? null : (part / whole) * 100;

/* ------------------------------------------------------- the path columns */

/* Path A's and path B's rung codes. They are NOT in the registry — neither path
 * is built, so neither has a `PathDef` — but the 2026-09-10 (a) ruling names
 * them while mapping them onto path C ("A1 → C1 = town + disposition; A2 → not
 * applicable; A3 → C3 = town + area"), and the wave text asks for a path-B
 * column "from day one". They are listed here as bare codes with no invented
 * labels: the column group carries the path's own explanation, straight from
 * the backend's registry. */
const UNBUILT_RUNGS: Record<string, string[]> = { A: ['A1', 'A2', 'A3'], B: ['B'] };

interface Column {
  key: string;
  built: boolean;
  label: string;
  title: string;
}

function columnsFor(paths: NewDedupCandidatePath[]): Column[] {
  const out: Column[] = [];
  for (const p of paths) {
    if (p.built && p.rungs.length) {
      for (const r of p.rungs) {
        out.push({
          key: r.code,
          built: true,
          label: r.code,
          title: `${r.code} — ${r.label}. ${r.explanation}`,
        });
      }
      continue;
    }
    for (const code of UNBUILT_RUNGS[p.code] ?? [p.code]) {
      out.push({
        key: code,
        built: false,
        label: code,
        title: `${code} — path ${p.code} (${p.label}) is not built. ${p.explanation}`,
      });
    }
  }
  return out;
}

/* --------------------------------------------------------------- the page */

export default function NewDedupCandidates() {
  const [params, setParams] = useSearchParams();
  const raw = params.get('generation_id');
  /* Only a plain integer is a run id. Anything else is treated as absent, so a
   * hand-edited URL falls back to the newest successful run instead of asking
   * the API a question it would answer with a 422. */
  const generationId = raw != null && /^\d+$/.test(raw) ? Number(raw) : null;

  const q = useQuery({
    queryKey: ['new-dedup', 'candidates', 'overview', generationId],
    queryFn: () => getNewDedupCandidateOverview(generationId),
    /* Switching runs must not blank the page — least of all the picker that did
     * the switching. The previous run stays on screen, dimmed, until the new one
     * arrives. */
    placeholderData: keepPreviousData,
  });

  const data = q.data?.data;
  const stats = data?.stats ?? null;

  const pickRun = (value: string) => {
    const next = new URLSearchParams(params);
    if (value === '') next.delete('generation_id');
    else next.set('generation_id', value);
    setParams(next, { replace: false });
  };

  return (
    <div className="px-6 pt-5 pb-10 max-w-screen-xl mx-auto">
      <header>
        <h1 className="text-2xl leading-tight">NEW DEDUP · Candidates</h1>
        <p className="mt-1 text-sm text-[var(--color-ink-2)] leading-relaxed max-w-[52rem]">
          Before the program decides that two listings are the same property, it draws up a
          shortlist: every pair of listings that is even worth comparing. That shortlist is what
          this page audits. A <em>candidate pair</em> is two listings the rule thinks might be the
          same property — it is a shortlist, not a verdict. The question to hold the numbers
          against is the one Gate 2 asks: does anything rightful fall out of this shortlist
          because of missing data rather than because it genuinely does not match?
        </p>
      </header>

      {q.isPending && (
        <p className="mt-6 flex items-center gap-2 text-sm text-[var(--color-ink-3)]">
          <Spinner /> Loading the audit…
        </p>
      )}
      {!q.isPending && q.isFetching && (
        <p className="mt-3 flex items-center gap-2 text-[0.72rem] text-[var(--color-ink-3)]">
          <Spinner size={9} /> Loading the run you picked…
        </p>
      )}
      {q.error && (
        <>
          <ErrorBanner message={(q.error as Error).message} />
          {/* A hand-edited run id that does not exist would otherwise be a dead
            * end: the error replaces the page, picker included. */}
          {generationId != null && (
            <p className="mt-2 text-[0.75rem]">
              <button
                type="button"
                onClick={() => pickRun('')}
                className="text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
              >
                Back to the newest finished run
              </button>
            </p>
          )}
        </>
      )}

      {data && (
        <div className={`mt-5 space-y-4 ${q.isFetching ? 'opacity-60' : ''}`}>
          <RunPicker
            recent={data.recent}
            selected={generationId}
            generation={data.generation}
            onPick={pickRun}
            storeReady={data.store_ready}
          />

          {/* --- the three empty states, in the order they can occur --- */}
          {!data.store_ready && (
            <Notice title="The candidate store has not been created yet.">
              <p>
                The tables that hold candidate pairs arrive with migration 492, and that migration
                has not been applied to this database. A <em>migration</em> is one numbered file of
                schema changes; until it runs, there is nowhere for a candidate pair to be written,
                so there is nothing here to audit. Everything else on this page — the paths, what
                each one looks for — is code, not data, and is shown below regardless.
              </p>
            </Notice>
          )}

          {data.store_ready && !data.generation && (
            <Notice title="No candidate run has finished yet.">
              <p>
                The store exists, but the generation lane has not produced a run. A{' '}
                <em>generation</em> is one pass of the candidate rule over the whole corpus under
                one set of parameters; the numbers on this page are computed once, at the end of
                such a pass. Until one runs, every figure below would be a fabrication.
              </p>
            </Notice>
          )}

          {data.generation && !stats && (
            <Notice title="This run produced no audit numbers.">
              <p>
                Run #{data.generation.id} is <strong>{data.generation.status}</strong>. The
                statistics are computed in one step at the very end of a run, so a run that is
                still going — or that failed before reaching that step — carries none.
                {data.generation.error_message && (
                  <>
                    {' '}
                    It reported: <span className="font-mono">{data.generation.error_message}</span>
                  </>
                )}
              </p>
            </Notice>
          )}

          {stats && (
            <>
              <Card
                title="Funnel — from every listing to a candidate"
                lede="Each step is a smaller set than the one above it, and the gap between two steps is what was lost there. Read it top to bottom: if a step drops far more than you expect, that step is where data quality — not the rule — is deciding who gets a chance to be matched."
              >
                <CandidateFunnel stats={stats} />
              </Card>

              <Card
                title="Property type × path"
                lede="Which pairs the run actually produced, split by what kind of property they are and by which path found them. A PATH is one way of looking for pairs: path C (built) starts from the town, path A would have started from a street or a radius in metres, path B will start from the photographs. Paths A and B keep their columns here and show an em dash, because a column that is missing hides the gap while an empty one names it."
              >
                <TypePathMatrix stats={stats} paths={data.paths} />
              </Card>

              <Card
                title="Missing data — overall"
                lede="The same losses as the funnel, named as what is absent rather than as what survived. Every count is shown with the share it represents and the group that share is out of, because two of these rows have a different denominator: the floor row is a share of apartments, not of all listings."
              >
                <MissingOverall stats={stats} />
              </Card>

              <Card
                title="Missing data — per portal, per property type"
                lede="The same gaps again, one row per portal and property type, so a portal that systematically omits a field shows up as a column of low percentages rather than being averaged away. Click any column heading to sort by it."
              >
                <MissingPerPortal rows={stats.funnel ?? []} />
              </Card>

              <Card
                title="Town statistics"
                lede="Path C compares two listings only when they sit in the same town, so the town is the unit where a 'candidate storm' — one place producing an unreasonable share of all pairs — would appear first. These four readouts are the town-level stand-in for the pin and clique statistics the design asked for."
              >
                <TownStatistics stats={stats} />
              </Card>

              <Card
                title="The parameter set this run used"
                lede="Every number above was computed under exactly these knobs. The fingerprint is a short hash of them: two runs with the same fingerprint are directly comparable, two with different fingerprints are not — they are answers to different questions."
              >
                <ParameterSet stats={stats} generation={data.generation} />
              </Card>
            </>
          )}

          <Card
            title="What the paths are"
            lede="The vocabulary the rest of the page uses, straight from the program's own registry. A PATH is one way of looking for pairs; a RUNG is which attributes a pair was compared on. A pair sits on exactly one rung."
          >
            <PathRegistry paths={data.paths} />
          </Card>
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------ run picker */

function RunPicker({
  recent,
  selected,
  generation,
  onPick,
  storeReady,
}: {
  recent: NewDedupCandidateRecentGeneration[];
  selected: number | null;
  generation: NewDedupCandidateGeneration | null;
  onPick: (value: string) => void;
  storeReady: boolean;
}) {
  return (
    <section className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <label
          htmlFor="generation-picker"
          className="text-[0.62rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]"
        >
          Run
        </label>
        <select
          id="generation-picker"
          aria-label="Run"
          value={selected == null ? '' : String(selected)}
          disabled={!storeReady || recent.length === 0}
          onChange={(e) => onPick(e.target.value)}
          className="px-2 py-1 text-sm rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)] text-[var(--color-ink)] disabled:opacity-60"
        >
          <option value="">Newest finished run</option>
          {recent.map((r) => (
            <option key={r.id} value={String(r.id)}>
              #{r.id} · {r.status}
              {r.pairs_total != null ? ` · ${fmtCount(r.pairs_total)} pairs` : ''}
              {r.partial ? ' · partial' : ''}
              {r.created_at ? ` · ${fmtAbsolute(r.created_at)}` : ''}
            </option>
          ))}
        </select>
        {generation && (
          <span className="text-[0.72rem] text-[var(--color-ink-3)]">
            showing run #{generation.id} ({generation.status}), parameter set{' '}
            <span className="font-mono">{generation.fingerprint}</span>
          </span>
        )}
        {storeReady && recent.length === 0 && (
          <span className="text-[0.72rem] text-[var(--color-ink-3)]">no runs to pick from yet</span>
        )}
      </div>
      <p className="mt-1.5 text-[0.72rem] leading-relaxed text-[var(--color-ink-3)]">
        Each entry is one pass of the candidate rule over the corpus. Picking one puts it in this
        page's address, so a run is a link you can keep or send — that is how two parameter sets
        are compared: open the page twice.
      </p>
    </section>
  );
}

/* ---------------------------------------------------------- the matrix */

interface MatrixRow {
  key: string;
  typeLo: string | null;
  typeHi: string | null;
  deal: string | null;
  perRung: Record<string, number>;
  total: number;
  floorChecked: number;
}

function groupMatrix(rows: NewDedupCandidateMatrixRow[]): MatrixRow[] {
  const byKey = new Map<string, MatrixRow>();
  for (const r of rows) {
    const key = `${r.category_main_lo ?? ''}|${r.category_main_hi ?? ''}|${r.category_type ?? ''}`;
    const entry = byKey.get(key) ?? {
      key,
      typeLo: r.category_main_lo,
      typeHi: r.category_main_hi,
      deal: r.category_type,
      perRung: {},
      total: 0,
      floorChecked: 0,
    };
    entry.perRung[r.rung] = (entry.perRung[r.rung] ?? 0) + (r.pairs || 0);
    entry.total += r.pairs || 0;
    entry.floorChecked += r.floor_checked || 0;
    byKey.set(key, entry);
  }
  return [...byKey.values()].sort((a, b) => b.total - a.total);
}

function TypePathMatrix({
  stats,
  paths,
}: {
  stats: NewDedupCandidateStats;
  paths: NewDedupCandidatePath[];
}) {
  const columns = useMemo(() => columnsFor(paths), [paths]);
  const rows = useMemo(() => groupMatrix(stats.matrix ?? []), [stats.matrix]);
  const totalPairs = rows.reduce((a, r) => a + r.total, 0);
  const totalFloorChecked = rows.reduce((a, r) => a + r.floorChecked, 0);

  if (!rows.length) {
    return <p className="text-sm text-[var(--color-ink-3)]">This run produced no pairs.</p>;
  }

  return (
    <>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className={HEAD}>
              <th className={TH}>Property type</th>
              <th className={TH}>Deal</th>
              {columns.map((c) => (
                <th
                  key={c.key}
                  title={c.title}
                  className={`${TH} text-right ${
                    c.built ? '' : 'text-[var(--color-ink-4)] font-normal'
                  }`}
                >
                  {c.label}
                </th>
              ))}
              <th className={`${TH} text-right`}>All pairs</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key} className={ROW}>
                <td className={TD}>
                  {typeLabel(r.typeLo)}
                  {r.typeHi !== r.typeLo && (
                    <>
                      {' '}
                      <span className="text-[var(--color-ink-3)]">↔</span> {typeLabel(r.typeHi)}
                    </>
                  )}
                </td>
                <td className={`${TD} text-[var(--color-ink-2)]`}>{dealLabel(r.deal)}</td>
                {columns.map((c) => (
                  <td
                    key={c.key}
                    className={`${NUM} ${c.built ? '' : 'text-[var(--color-ink-4)]'}`}
                  >
                    {c.built ? fmtCount(r.perRung[c.key] ?? 0) : '—'}
                  </td>
                ))}
                <td className={`${NUM} font-medium`}>{fmtCount(r.total)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-3 text-[0.72rem] leading-relaxed text-[var(--color-ink-3)]">
        The apartment floor rule — two apartments more than the allowed number of floors apart are
        not the same flat — could be applied to{' '}
        <span className="font-mono tabular-nums">{fmtCount(totalFloorChecked)}</span> of the{' '}
        <span className="font-mono tabular-nums">{fmtCount(totalPairs)}</span> pairs (
        {fmtPct(share(totalFloorChecked, totalPairs))}). For the rest, at least one side is not an
        apartment or states no floor; those pairs are kept rather than dropped, so a missing floor
        never costs a rightful candidate.
      </p>
    </>
  );
}

/* --------------------------------------------------------- missing data */

interface MissingRow {
  label: string;
  count: number;
  basis: string;
  basisCount: number;
  explanation: string;
}

function missingRows(stats: NewDedupCandidateStats): MissingRow[] {
  const f = stats.funnel ?? [];
  const sum = (pick: (r: NewDedupCandidateFunnelRow) => number): number =>
    f.reduce((acc, r) => acc + (pick(r) || 0), 0);

  const listings = sum((r) => r.listings);
  const byt = sum((r) => r.byt);

  return [
    {
      label: 'No answer from the location engine at all',
      count: listings - sum((r) => r.with_projection),
      basis: 'of all listings',
      basisCount: listings,
      explanation:
        'The location engine has no row for this listing — most often because the listing predates the portal sweep that would have produced one.',
    },
    {
      label: 'An answer, but too coarse to name a town',
      count: sum((r) => r.with_projection) - sum((r) => r.with_town),
      basis: 'of all listings',
      basisCount: listings,
      explanation:
        'The engine placed the listing, but only at a grain above the town (a district or a region). Path C needs the town itself, so these cannot be paired by it.',
    },
    {
      label: 'A town, but neither a disposition nor an area',
      count: sum((r) => r.town_no_attribute),
      basis: 'of all listings',
      basisCount: listings,
      explanation:
        'The listing is in a town the rule can block on, but states nothing the rule can compare — no disposition (2+kk and the like) and no floor area. These are the candidates data quality costs outright.',
    },
    {
      label: 'No disposition stated',
      count: listings - sum((r) => r.with_disposition),
      basis: 'of all listings',
      basisCount: listings,
      explanation:
        'The rule falls back to comparing floor areas for these — the fallback is on absence only, never on two dispositions that simply disagree.',
    },
    {
      label: 'No floor area stated',
      count: listings - sum((r) => r.with_area),
      basis: 'of all listings',
      basisCount: listings,
      explanation:
        'A zero area counts as missing here: a zero is a blank the parser wrote, not a measurement. For land, the area compared is the plot area.',
    },
    {
      label: 'Apartments with no floor stated',
      count: byt - sum((r) => r.byt_with_floor),
      basis: 'of apartments',
      basisCount: byt,
      explanation:
        'The floor rule is checked only when both sides are apartments with a known floor. A missing floor keeps the pair rather than dropping it — the count above says how often that happens.',
    },
  ];
}

function MissingOverall({ stats }: { stats: NewDedupCandidateStats }) {
  const rows = missingRows(stats);
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className={HEAD}>
            <th className={TH}>What is missing</th>
            <th className={`${TH} text-right`}>Listings</th>
            <th className={`${TH} text-right`}>Share</th>
            <th className={TH}>Share of</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.label} className={ROW}>
              <td className={TD}>
                <div>{r.label}</div>
                <p className="mt-0.5 text-[0.72rem] leading-relaxed text-[var(--color-ink-3)] max-w-[38rem]">
                  {r.explanation}
                </p>
              </td>
              <td className={`${NUM} align-top`}>{fmtCount(r.count)}</td>
              <td className={`${NUM} align-top`}>{fmtPct(share(r.count, r.basisCount))}</td>
              <td className={`${TD} align-top text-[0.72rem] text-[var(--color-ink-3)] whitespace-nowrap`}>
                {r.basis} ({fmtCount(r.basisCount)})
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

type PortalSortKey =
  | 'source'
  | 'category_main'
  | 'category_type'
  | 'listings'
  | 'with_town'
  | 'with_disposition'
  | 'with_area'
  | 'c1_eligible'
  | 'c3_eligible'
  | 'town_no_attribute';

const PORTAL_COLUMNS: { key: PortalSortKey; label: string; numeric: boolean; pct: boolean }[] = [
  { key: 'source', label: 'Portal', numeric: false, pct: false },
  { key: 'category_main', label: 'Property type', numeric: false, pct: false },
  { key: 'category_type', label: 'Deal', numeric: false, pct: false },
  { key: 'listings', label: 'Listings', numeric: true, pct: false },
  { key: 'with_town', label: 'With a town', numeric: true, pct: true },
  { key: 'with_disposition', label: 'With a disposition', numeric: true, pct: true },
  { key: 'with_area', label: 'With an area', numeric: true, pct: true },
  { key: 'c1_eligible', label: 'Can take C1', numeric: true, pct: true },
  { key: 'c3_eligible', label: 'Can take C3', numeric: true, pct: true },
  { key: 'town_no_attribute', label: 'Town, nothing to compare', numeric: true, pct: true },
];

function MissingPerPortal({ rows }: { rows: NewDedupCandidateFunnelRow[] }) {
  const [sort, setSort] = useState<{ key: PortalSortKey; desc: boolean }>({
    key: 'listings',
    desc: true,
  });

  const sorted = useMemo(() => {
    const copy = [...rows];
    copy.sort((a, b) => {
      const av = a[sort.key];
      const bv = b[sort.key];
      if (typeof av === 'number' && typeof bv === 'number') {
        return sort.desc ? bv - av : av - bv;
      }
      const as = av == null ? '' : String(av);
      const bs = bv == null ? '' : String(bv);
      return sort.desc ? bs.localeCompare(as) : as.localeCompare(bs);
    });
    return copy;
  }, [rows, sort]);

  if (!rows.length) {
    return <p className="text-sm text-[var(--color-ink-3)]">This run counted no listings.</p>;
  }

  const toggle = (key: PortalSortKey) =>
    setSort((s) => (s.key === key ? { key, desc: !s.desc } : { key, desc: true }));

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className={HEAD}>
            {PORTAL_COLUMNS.map((c) => (
              <th
                key={c.key}
                aria-sort={sort.key === c.key ? (sort.desc ? 'descending' : 'ascending') : 'none'}
                className={`${TH} ${c.numeric ? 'text-right' : ''}`}
              >
                <button
                  type="button"
                  onClick={() => toggle(c.key)}
                  className="uppercase tracking-[0.1em] hover:text-[var(--color-copper-2)]"
                >
                  {c.label}
                  {sort.key === c.key && (
                    <span aria-hidden className="ml-1">
                      {sort.desc ? '▾' : '▴'}
                    </span>
                  )}
                </button>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((r) => (
            <tr key={`${r.source}|${r.category_main ?? ''}|${r.category_type ?? ''}`} className={ROW}>
              <td className={TD}>{dash(r.source)}</td>
              <td className={TD}>{typeLabel(r.category_main)}</td>
              <td className={`${TD} text-[var(--color-ink-2)]`}>{dealLabel(r.category_type)}</td>
              <td className={NUM}>{fmtCount(r.listings)}</td>
              <Cell value={r.with_town} of={r.listings} />
              <Cell value={r.with_disposition} of={r.listings} />
              <Cell value={r.with_area} of={r.listings} />
              <Cell value={r.c1_eligible} of={r.listings} />
              <Cell value={r.c3_eligible} of={r.listings} />
              <Cell value={r.town_no_attribute} of={r.listings} />
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* A count and, under it, what share of that row's listings it is — the count
 * alone cannot be compared across portals of wildly different size. */
function Cell({ value, of }: { value: number | null; of: number }) {
  return (
    <td className={NUM}>
      {fmtCount(value)}
      <div className="text-[0.68rem] text-[var(--color-ink-3)]">{fmtPct(share(value, of))}</div>
    </td>
  );
}

/* ------------------------------------------------------ town statistics */

function bandLabel(from: number, to: number | null): string {
  if (to == null) return `${fmtCount(from)}+`;
  if (from === to) return fmtCount(from);
  return `${fmtCount(from)}–${fmtCount(to)}`;
}

/* The zero band is NOT a measurement on a generation. The histogram the lane
 * writes onto the generation row is built from the pair rows, so a town that
 * produced no pair never enters it and the 0 band is structurally 0 — printing
 * that 0 would read as "every town produced a pair", which is the opposite of
 * what is known. It renders as a gap, like every other number nobody measured.
 * (scripts/dedup_candidates_generate.py:generation_stats feeds `_distribution`
 * the ranked pair rows; only estimate mode feeds it every town.) */
const isUnmeasuredBand = (from: number, to: number | null): boolean => from === 0 && to === 0;

function TownStatistics({ stats }: { stats: NewDedupCandidateStats }) {
  const distribution = stats.distribution ?? [];
  const maxTowns = distribution.reduce((m, d) => Math.max(m, d.towns || 0), 0);
  const topTowns = stats.top_towns ?? [];
  const buckets = stats.top_buckets ?? [];
  const assignment = stats.town_assignment ?? [];
  const assignedTotal = assignment.reduce((a, r) => a + (r.listings || 0), 0);

  return (
    <div className="space-y-6">
      <p className="text-sm text-[var(--color-ink)]">
        Towns that produced at least one pair:{' '}
        <strong className="font-mono tabular-nums font-medium">
          {fmtCount(stats.towns_with_pairs)}
        </strong>
      </p>

      <div>
        <h3 className="text-[0.68rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
          How many towns produced how many pairs
        </h3>
        <p className="mt-1 text-[0.72rem] leading-relaxed text-[var(--color-ink-3)] max-w-[46rem]">
          A histogram over towns: each band is a range of pair counts, and the number beside it is
          how many towns fall in that band. A healthy shape is most towns near the bottom and very
          few at the top; a fat top band is a candidate storm, where one or two places generate a
          disproportionate share of all the work. Towns that produced no pair at all are not
          counted here — only towns with at least one pair reach this histogram, so the 0 band is
          left blank rather than shown as a measured zero.
        </p>
        {distribution.length === 0 ? (
          <p className="mt-2 text-sm text-[var(--color-ink-3)]">Not computed for this run.</p>
        ) : (
          <ul className="mt-2 space-y-1.5">
            {distribution.map((d) => {
              const unmeasured = isUnmeasuredBand(d.pairs_from, d.pairs_to);
              const label = bandLabel(d.pairs_from, d.pairs_to);
              return (
                <li key={`${d.pairs_from}-${d.pairs_to ?? 'max'}`} className="flex items-center gap-3">
                  <span className="w-32 shrink-0 text-right font-mono tabular-nums text-[0.72rem] text-[var(--color-ink-2)]">
                    {label}
                  </span>
                  <span className="flex-1 min-w-0">
                    <ProportionBar
                      value={unmeasured ? null : d.towns}
                      base={maxTowns}
                      title={
                        unmeasured
                          ? `${label} pairs: not counted — a town with no pair never reaches this histogram`
                          : `${label} pairs: ${fmtCount(d.towns)} towns`
                      }
                    />
                  </span>
                  <span className="w-20 shrink-0 text-right font-mono tabular-nums text-[0.72rem] text-[var(--color-ink-2)]">
                    {unmeasured ? '—' : fmtCount(d.towns)}
                  </span>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <div>
        <h3 className="text-[0.68rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
          The towns that produced the most pairs
        </h3>
        <p className="mt-1 text-[0.72rem] leading-relaxed text-[var(--color-ink-3)] max-w-[46rem]">
          The top of that histogram, named. The code is the RÚIAN municipality code the location
          engine blocks on; a town with no name here simply has none recorded on the projection.
        </p>
        {topTowns.length === 0 ? (
          <p className="mt-2 text-sm text-[var(--color-ink-3)]">Not computed for this run.</p>
        ) : (
          <div className="mt-2 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className={HEAD}>
                  <th className={TH}>Town</th>
                  <th className={TH}>Code</th>
                  <th className={`${TH} text-right`}>Listings</th>
                  <th className={`${TH} text-right`}>C1 pairs</th>
                  <th className={`${TH} text-right`}>C3 pairs</th>
                  <th className={`${TH} text-right`}>All pairs</th>
                </tr>
              </thead>
              <tbody>
                {topTowns.map((t) => (
                  <tr key={t.block_key} className={ROW}>
                    <td className={TD}>{dash(t.obec_name)}</td>
                    <td className={`${TD} font-mono text-[0.72rem] text-[var(--color-ink-3)]`}>
                      {t.block_key}
                    </td>
                    <td className={NUM}>{fmtCount(t.listings ?? null)}</td>
                    <td className={NUM}>{fmtCount(t.C1)}</td>
                    <td className={NUM}>{fmtCount(t.C3)}</td>
                    <td className={`${NUM} font-medium`}>{fmtCount((t.C1 || 0) + (t.C3 || 0))}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div>
        <h3 className="text-[0.68rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
          The biggest town + disposition buckets
        </h3>
        <p className="mt-1 text-[0.72rem] leading-relaxed text-[var(--color-ink-3)] max-w-[46rem]">
          Counted from the listing side, before any pairing: how many listings share one town and
          one disposition. This is where the work comes from — a bucket of n listings can produce
          up to n × (n − 1) ÷ 2 pairs, so a very large bucket is worth seeing before it is felt.
        </p>
        {buckets.length === 0 ? (
          <p className="mt-2 text-sm text-[var(--color-ink-3)]">Not computed for this run.</p>
        ) : (
          <div className="mt-2 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className={HEAD}>
                  <th className={TH}>Town</th>
                  <th className={TH}>Disposition</th>
                  <th className={`${TH} text-right`}>Listings</th>
                  <th className={`${TH} text-right`}>Still active</th>
                </tr>
              </thead>
              <tbody>
                {buckets.map((b) => (
                  <tr key={`${b.obec_kod}|${b.disposition ?? ''}`} className={ROW}>
                    <td className={TD}>
                      {dash(b.obec_name)}{' '}
                      <span className="font-mono text-[0.68rem] text-[var(--color-ink-3)]">
                        {b.obec_kod}
                      </span>
                    </td>
                    <td className={TD}>{dash(b.disposition)}</td>
                    <td className={NUM}>{fmtCount(b.listings)}</td>
                    <td className={NUM}>{fmtCount(b.active)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div>
        <h3 className="text-[0.68rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">
          How the town was decided
        </h3>
        <p className="mt-1 text-[0.72rem] leading-relaxed text-[var(--color-ink-3)] max-w-[46rem]">
          The location engine can arrive at a town several ways — by dropping the coordinates into
          the municipality's boundary, by trusting what the portal claimed, and so on. This is a
          breakdown for the audit, never a gate: no method is excluded from path C.
        </p>
        {assignment.length === 0 ? (
          <p className="mt-2 text-sm text-[var(--color-ink-3)]">Not computed for this run.</p>
        ) : (
          <div className="mt-2 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className={HEAD}>
                  <th className={TH}>Method</th>
                  <th className={`${TH} text-right`}>Listings</th>
                  <th className={`${TH} text-right`}>Share</th>
                </tr>
              </thead>
              <tbody>
                {assignment.map((a) => (
                  <tr key={a.method ?? 'unknown'} className={ROW}>
                    <td className={`${TD} font-mono text-[0.8rem]`}>{dash(a.method)}</td>
                    <td className={NUM}>{fmtCount(a.listings)}</td>
                    <td className={NUM}>{fmtPct(share(a.listings, assignedTotal))}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------- parameter set */

function ParameterSet({
  stats,
  generation,
}: {
  stats: NewDedupCandidateStats;
  generation: NewDedupCandidateGeneration | null;
}) {
  const inputs = generation?.inputs ?? {};
  const keys = Object.keys(inputs).sort();

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className={HEAD}>
              <th className={TH}>Knob</th>
              <th className={TH}>Value</th>
            </tr>
          </thead>
          <tbody>
            {keys.length === 0 && (
              <tr className={ROW}>
                <td className={`${TD} text-[var(--color-ink-3)]`} colSpan={2}>
                  Not recorded for this run.
                </td>
              </tr>
            )}
            {keys.map((k) => (
              <tr key={k} className={ROW}>
                <td className={`${TD} font-mono text-[0.78rem]`}>{k}</td>
                <td className={`${TD} font-mono text-[0.78rem]`}>{String(inputs[k])}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <dl className="text-sm space-y-1.5">
        <Fact label="Scope" value={dash(stats.scope)} />
        <Fact
          label="Covered the whole corpus"
          value={stats.partial == null ? '—' : stats.partial ? 'no — a limited pilot' : 'yes'}
        />
        <Fact label="Pairs written" value={fmtCount(stats.pairs_upserted ?? null)} />
        <Fact label="Stale pairs removed" value={fmtCount(stats.stale_deleted ?? null)} />
        <Fact label="Started" value={fmtAbsolute(generation?.started_at)} />
        <Fact label="Finished" value={fmtAbsolute(generation?.completed_at)} />
      </dl>
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4 border-b border-[var(--color-rule-soft)] pb-1">
      <dt className="text-[var(--color-ink-3)]">{label}</dt>
      <dd className="font-mono tabular-nums text-[0.8rem]">{value}</dd>
    </div>
  );
}

/* ------------------------------------------------------ path registry */

function PathRegistry({ paths }: { paths: NewDedupCandidatePath[] }) {
  return (
    <ul className="space-y-3">
      {paths.map((p) => (
        <li key={p.code}>
          <div className="flex flex-wrap items-baseline gap-2">
            <span className="text-sm font-medium">Path {p.code}</span>
            <span className="text-sm text-[var(--color-ink-2)]">{p.label}</span>
            <span
              className={[
                'text-[0.6rem] tracking-[0.1em] uppercase px-1.5 py-0.5 rounded-[var(--radius-xs)]',
                p.built
                  ? 'bg-[var(--color-sage-soft)] text-[var(--color-sage)]'
                  : 'bg-[var(--color-ochre-soft)] text-[var(--color-ochre)]',
              ].join(' ')}
            >
              {p.built ? 'built' : 'not built'}
            </span>
          </div>
          <p className="mt-1 text-[0.75rem] leading-relaxed text-[var(--color-ink-3)] max-w-[52rem]">
            {p.explanation}
          </p>
          {p.rungs.length > 0 && (
            <ul className="mt-1.5 space-y-1">
              {p.rungs.map((r) => (
                <li key={r.code} className="text-[0.75rem] leading-relaxed">
                  <span className="font-mono text-[var(--color-ink-2)]">{r.code}</span>{' '}
                  <span className="text-[var(--color-ink-2)]">{r.label}</span>
                  <span className="text-[var(--color-ink-3)]"> — {r.explanation}</span>
                </li>
              ))}
            </ul>
          )}
        </li>
      ))}
    </ul>
  );
}
