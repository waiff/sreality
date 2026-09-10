/* Location compare (location program W6) — the operator's review bench for the
 * dark filter + map cutover.
 *
 * OLD is what Browse shows today (`browse_list`, property grain). NEW is the
 * serving projection. Nothing on this page writes, and nothing on it is the
 * cutover: it exists so the flip is approved from evidence rather than from a
 * summary statistic.
 *
 * The vocabulary is the design's, not a UI invention (05 §5.3.3 A): membership
 * in an admin unit is decided by the ASSIGNMENT, so a row is `certain`,
 * `possible` (shown, badged), `no`, or has no projection row at all — and every
 * aggregate here publishes that whole denominator tuple (§5.5.3), because a
 * bare "agreement %" without the excluded counts is the exact lie the design
 * forbids. */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { Field, PickButton } from '@/components/controls';
import { ApiError } from '@/lib/api';
import { fmtCount } from '@/lib/format';
import { propertyListingPath } from '@/lib/listingUrl';
import CompareMapPair, {
  BUCKET_LABELS, useBucketColors, type Centre,
} from '@/components/location-compare/CompareMapPair';
import {
  DEFAULT_KRAJE, KRAJ_LABELS, PRAHA_KOD, REASON_LABELS, STREDOCESKY_KOD,
  fetchCompareMap, fetchCompareRadius, fetchCompareScope, fetchCompareStreets,
  fetchCompareUnit, fetchCompareUnits,
  type Bbox, type CompareRow, type MapRow, type MethodRow, type SourceRow,
  type StreetRow, type UnitLevel,
} from '@/lib/locationCompare';

/* ---------- shared bits (LocationQuality idiom) ---------- */

function Card({ title, accessory, children }: {
  title: string; accessory?: React.ReactNode; children: React.ReactNode;
}) {
  return (
    <section className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)] font-medium">
          {title}
        </h2>
        {accessory ?? null}
      </div>
      <div className="mt-3">{children}</div>
    </section>
  );
}

type Tone = 'ink' | 'sage' | 'copper' | 'ochre' | 'brick';

const TONE_TEXT: Record<Tone, string> = {
  ink: 'text-[var(--color-ink)]',
  sage: 'text-[var(--color-sage)]',
  copper: 'text-[var(--color-copper)]',
  ochre: 'text-[var(--color-ochre)]',
  brick: 'text-[var(--color-brick)]',
};

function Stat({ label, value, hint, tone = 'ink' }: {
  label: string; value: string; hint?: string; tone?: Tone;
}) {
  return (
    <div className="rounded-[var(--radius-sm)] border border-[var(--color-rule-soft)] bg-[var(--color-paper-2)] px-3 py-2">
      <div className="text-[0.62rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]">{label}</div>
      <div className={`font-mono tabular-nums text-xl ${TONE_TEXT[tone]}`}>{value}</div>
      {hint ? <div className="text-[0.68rem] text-[var(--color-ink-4)]">{hint}</div> : null}
    </div>
  );
}

const BADGE_TONE: Record<Tone, string> = {
  ink: 'bg-[var(--color-inset)] text-[var(--color-ink-2)]',
  sage: 'bg-[var(--color-sage-soft)] text-[var(--color-sage)]',
  copper: 'bg-[var(--color-copper-soft)] text-[var(--color-copper)]',
  ochre: 'bg-[var(--color-ochre-soft)] text-[var(--color-ochre)]',
  brick: 'bg-[var(--color-brick-soft)] text-[var(--color-brick)]',
};

function Badge({ tone = 'ink', children }: { tone?: Tone; children: React.ReactNode }) {
  return (
    <span className={`inline-block rounded-full px-2 py-0.5 text-[0.62rem] font-medium ${BADGE_TONE[tone]}`}>
      {children}
    </span>
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

function Skeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="animate-pulse space-y-2" aria-hidden>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="h-8 rounded-[var(--radius-sm)] bg-[var(--color-inset)]" />
      ))}
    </div>
  );
}

function Empty({ text = 'No rows.' }: { text?: string }) {
  return <p className="text-sm text-[var(--color-ink-3)]">{text}</p>;
}

const TH = 'py-1.5 pr-3 font-medium';
const HEAD = 'text-left text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]';
const NUM = 'py-1.5 pr-3 text-right font-mono tabular-nums';

function useDebounced<T>(value: T, ms: number): T {
  const [held, setHeld] = useState(value);
  useEffect(() => {
    const t = window.setTimeout(() => setHeld(value), ms);
    return () => window.clearTimeout(t);
  }, [value, ms]);
  return held;
}

const pct = (n: number | null | undefined): string => (n == null ? '—' : `${n.toFixed(1)}%`);

/* ---------- scope bar ---------- */

const KRAJ_CHIPS = [PRAHA_KOD, STREDOCESKY_KOD] as const;

function OkresPicker({ options, value, onChange }: {
  options: { code: number; name: string | null }[];
  value: number | null;
  onChange: (code: number | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      if (!boxRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', away);
    return () => document.removeEventListener('mousedown', away);
  }, [open]);
  const current = options.find((o) => o.code === value);
  return (
    <div className="relative" ref={boxRef}>
      <button
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-2.5 py-1.5 text-xs text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)]"
      >
        {current ? `Okres: ${current.name ?? current.code}` : 'All okresy'}
      </button>
      {open ? (
        <div
          role="listbox"
          className="absolute left-0 z-30 mt-1 max-h-72 w-60 overflow-y-auto rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-3)] py-1"
        >
          <button
            type="button" role="option" aria-selected={value == null}
            onClick={() => { onChange(null); setOpen(false); }}
            className="block w-full px-3 py-1.5 text-left text-xs text-[var(--color-ink-2)] hover:bg-[var(--color-copper-soft)]"
          >
            All okresy
          </button>
          {options.map((o) => (
            <button
              key={o.code} type="button" role="option" aria-selected={o.code === value}
              onClick={() => { onChange(o.code); setOpen(false); }}
              className="block w-full px-3 py-1.5 text-left text-xs text-[var(--color-ink-2)] hover:bg-[var(--color-copper-soft)]"
            >
              {o.name ?? o.code} <span className="text-[var(--color-ink-4)]">{o.code}</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/* ---------- section 1: admin-unit filters ---------- */

type UnitTableRow = {
  code: number;
  name: string | null;
  n_old: number;
  n_new_certain: number;
  n_new_possible: number;
  n_only_old: number;
  n_only_new: number;
  agreement_pct: number | null;
};

function MiniMethods({ rows }: { rows: MethodRow[] }) {
  if (!rows.length) return <Empty />;
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className={HEAD}>
          <th className={TH}>Assignment method</th>
          <th className={`${TH} text-right`}>Rows</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.admin_assignment_method ?? 'null'} className="border-t border-[var(--color-rule-soft)]">
            <td className="py-1.5 pr-3">{r.admin_assignment_method ?? '∅'}</td>
            <td className={NUM}>{fmtCount(r.n)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function MiniSources({ rows }: { rows: SourceRow[] }) {
  if (!rows.length) return <Empty />;
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className={HEAD}>
          <th className={TH}>Source</th>
          <th className={`${TH} text-right`}>Old</th>
          <th className={`${TH} text-right`}>Certain</th>
          <th className={`${TH} text-right`}>Possible</th>
          <th className={`${TH} text-right`}>No row</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.source} className="border-t border-[var(--color-rule-soft)]">
            <td className="py-1.5 pr-3">{r.source}</td>
            <td className={NUM}>{fmtCount(r.n_old)}</td>
            <td className={`${NUM} text-[var(--color-sage)]`}>{fmtCount(r.n_new_certain)}</td>
            <td className={`${NUM} text-[var(--color-copper)]`}>{fmtCount(r.n_new_possible)}</td>
            <td className={`${NUM} text-[var(--color-brick)]`}>{fmtCount(r.n_no_row)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function RowList({ title, rows, tone }: { title: string; rows: CompareRow[]; tone: Tone }) {
  return (
    <div>
      <div className="text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]">{title}</div>
      <div className="mt-2 max-h-96 overflow-y-auto divide-y divide-[var(--color-rule-soft)]">
        {rows.length === 0 ? <Empty /> : rows.map((r) => (
          <div key={`${r.property_id}-${r.listing_id ?? 0}`} className="py-2 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <Link
                to={propertyListingPath(r.property_id)}
                className="font-mono text-[0.75rem] text-[var(--color-copper)] underline decoration-dotted"
              >
                #{r.property_id}
              </Link>
              <span className="text-[var(--color-ink-3)]">{r.source ?? '—'}</span>
              <Badge tone={tone}>{REASON_LABELS[r.reason] ?? r.reason}</Badge>
            </div>
            <div className="mt-0.5 text-[0.72rem] text-[var(--color-ink-2)]">
              old: {r.old_label ?? '∅'} → new: {r.new_label ?? '∅'}
            </div>
            <div className="text-[0.68rem] text-[var(--color-ink-4)]">
              {r.granularity ?? '∅'} · {r.match_confidence ?? '∅'} · {r.admin_assignment_method ?? '∅'} ·
              {' '}radius {r.uncertainty_radius_m == null ? '—' : `${r.uncertainty_radius_m} m`} ·
              {' '}boundary {r.distance_to_nearest_boundary_m == null ? '—' : `${Math.round(r.distance_to_nearest_boundary_m)} m`}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function UnitDetail({ level, code, kraje }: {
  level: UnitLevel; code: number; kraje: number[];
}) {
  const q = useQuery({
    queryKey: ['loc-compare', 'unit', level, code, kraje.join(',')],
    queryFn: ({ signal }) => fetchCompareUnit(level, code, kraje, 200, signal),
    staleTime: 60_000,
  });
  if (q.error) return <ErrorBanner error={q.error} />;
  if (!q.data) return <Skeleton rows={4} />;
  const c = q.data.counts;
  return (
    <div className="mt-4 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-4 py-3">
      <div className="flex flex-wrap items-baseline gap-2">
        <h3 className="text-base">{q.data.name ?? `${level} ${code}`}</h3>
        <span className="text-[0.7rem] text-[var(--color-ink-4)]">{level} · kód {code}</span>
      </div>
      <div className="mt-3 grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-8 gap-2">
        <Stat label="Old in unit" value={fmtCount(c.n_old)} />
        <Stat label="New certain" value={fmtCount(c.n_new_certain)} tone="sage" />
        <Stat label="New possible" value={fmtCount(c.n_new_possible)} tone="copper" />
        <Stat label="New elsewhere" value={fmtCount(c.n_new_no)} tone="ochre" />
        <Stat label="No row" value={fmtCount(c.n_no_row)} tone="brick" />
        <Stat label="Only old" value={fmtCount(c.n_only_old)} tone="ochre" />
        <Stat label="Only new" value={fmtCount(c.n_only_new)} tone="brick" />
        <Stat label="Claimed" value={fmtCount(c.n_claimed)} hint="operator/portal claim" />
      </div>
      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <MiniMethods rows={q.data.by_method} />
        <MiniSources rows={q.data.by_source} />
      </div>
      <div className="mt-4 grid gap-6 lg:grid-cols-2">
        <RowList title="Old shows, new doesn't" rows={q.data.only_old} tone="ochre" />
        <RowList title="New shows, old doesn't" rows={q.data.only_new} tone="brick" />
      </div>
    </div>
  );
}

/* ---------- section 1b: street ---------- */

function StreetSection({ obecKod, obecName, kraje }: {
  obecKod: number; obecName: string | null; kraje: number[];
}) {
  const [q, setQ] = useState('');
  const [picked, setPicked] = useState<StreetRow | null>(null);
  const debounced = useDebounced(q, 300);
  const streets = useQuery({
    queryKey: ['loc-compare', 'streets', obecKod, debounced],
    queryFn: ({ signal }) => fetchCompareStreets(obecKod, debounced, 20, signal),
    enabled: debounced.trim().length >= 2,
    staleTime: 60_000,
  });
  return (
    <Card title={`Filters — street in ${obecName ?? obecKod}`}>
      <p className="text-[0.72rem] text-[var(--color-ink-4)]">
        The old side has no street code — Browse matches the street's name inside the
        locality text of the obec, which is why a street here can pull in neighbours.
        The new side matches the street CODE.
      </p>
      <div className="mt-3 max-w-md">
        <Field label="Street" as="control">
          <input
            className="w-full rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-inset)] px-2 py-1.5 text-sm"
            placeholder="type at least 2 letters"
            value={q}
            onChange={(e) => { setQ(e.target.value); setPicked(null); }}
          />
        </Field>
      </div>
      {streets.error ? <div className="mt-2"><ErrorBanner error={streets.error} /></div> : null}
      {debounced.trim().length >= 2 && !picked ? (
        streets.isLoading ? <div className="mt-2"><Skeleton rows={2} /></div> : (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {(streets.data?.rows ?? []).length === 0
              ? <Empty text="No street matches." />
              : streets.data!.rows.map((s) => (
                <PickButton key={s.ulice_kod} on={false} onClick={() => setPicked(s)}>
                  {s.name ?? s.ulice_kod}
                </PickButton>
              ))}
          </div>
        )
      ) : null}
      {picked ? (
        <>
          <div className="mt-3 flex items-center gap-2 text-sm">
            <span className="text-[var(--color-ink-2)]">{picked.name ?? picked.ulice_kod}</span>
            <button
              type="button"
              onClick={() => { setPicked(null); setQ(''); }}
              className="text-[0.7rem] text-[var(--color-copper)] underline decoration-dotted"
            >
              clear
            </button>
          </div>
          <UnitDetail level="street" code={picked.ulice_kod} kraje={kraje} />
        </>
      ) : null}
    </Card>
  );
}

/* ---------- section 2: maps ---------- */

function MapLegend() {
  const colors = useBucketColors();
  return (
    <div className="flex flex-wrap items-center gap-3 text-[0.7rem] text-[var(--color-ink-2)]">
      {(Object.keys(BUCKET_LABELS) as (keyof typeof BUCKET_LABELS)[]).map((b) => (
        <span key={b} className="inline-flex items-center gap-1.5">
          <span className="size-2 rounded-full" style={{ background: colors[b] }} aria-hidden />
          {BUCKET_LABELS[b]}
        </span>
      ))}
    </div>
  );
}

function DeltaTable({ rows, hoveredId, onHover }: {
  rows: MapRow[]; hoveredId: number | null; onHover: (id: number | null) => void;
}) {
  const worst = useMemo(
    () => rows.filter((r) => r.delta_m != null).slice(0, 40),
    [rows],
  );
  if (!worst.length) return <Empty text="No row has a position on both sides here." />;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className={HEAD}>
            <th className={TH}>Property</th>
            <th className={TH}>Source</th>
            <th className={`${TH} text-right`}>Delta m</th>
            <th className={TH}>Render old → new</th>
            <th className={TH}>Method</th>
            <th className={TH}>Collision</th>
            <th className={TH}>Disputed</th>
          </tr>
        </thead>
        <tbody>
          {worst.map((r) => (
            <tr
              key={r.property_id}
              onMouseEnter={() => onHover(r.property_id)}
              onMouseLeave={() => onHover(null)}
              className={`border-t border-[var(--color-rule-soft)] ${r.property_id === hoveredId ? 'bg-[var(--color-copper-soft)]' : ''}`}
            >
              <td className="py-1.5 pr-3">
                <Link
                  to={propertyListingPath(r.property_id)}
                  className="font-mono text-[0.75rem] text-[var(--color-copper)] underline decoration-dotted"
                >
                  #{r.property_id}
                </Link>
              </td>
              <td className="py-1.5 pr-3 text-[var(--color-ink-2)]">{r.source ?? '—'}</td>
              <td className={NUM}>{r.delta_m == null ? '—' : Math.round(r.delta_m)}</td>
              <td className="py-1.5 pr-3 text-[0.75rem] text-[var(--color-ink-2)]">
                point → {r.render_as ?? (r.renderable_as_point ? 'point' : 'circle')}
                <span className="text-[var(--color-ink-4)]"> · {r.granularity ?? '∅'}</span>
              </td>
              <td className="py-1.5 pr-3 text-[0.75rem]">{r.admin_assignment_method ?? '∅'}</td>
              <td className="py-1.5 pr-3 text-[0.75rem]">{r.pin_collision_class ?? '∅'}</td>
              <td className="py-1.5 pr-3">
                {r.location_disputed ? <Badge tone="brick">disputed</Badge> : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ---------- page ---------- */

export default function LocationCompare() {
  const [kraje, setKraje] = useState<number[]>([...DEFAULT_KRAJE]);
  const [okresKod, setOkresKod] = useState<number | null>(null);
  const [detail, setDetail] = useState<{ level: UnitLevel; code: number } | null>(null);
  const [obec, setObec] = useState<{ code: number; name: string | null } | null>(null);
  const [bbox, setBbox] = useState<Bbox | null>(null);
  const [hoveredId, setHoveredId] = useState<number | null>(null);
  const [centre, setCentre] = useState<Centre | null>(null);
  const [pickMode, setPickMode] = useState(false);
  const [radiusM, setRadiusM] = useState(1000);

  const krajeKey = kraje.join(',');
  const scope = useQuery({
    queryKey: ['loc-compare', 'scope', krajeKey],
    queryFn: ({ signal }) => fetchCompareScope(kraje, signal),
    staleTime: 60_000,
  });

  const units = useQuery({
    queryKey: ['loc-compare', 'units', okresKod, krajeKey],
    queryFn: ({ signal }) => fetchCompareUnits('obec', okresKod!, kraje, signal),
    enabled: okresKod != null,
    staleTime: 60_000,
  });

  const debouncedBbox = useDebounced(bbox, 400);
  const mapQ = useQuery({
    queryKey: [
      'loc-compare', 'map', krajeKey,
      debouncedBbox && [
        debouncedBbox.west.toFixed(3), debouncedBbox.south.toFixed(3),
        debouncedBbox.east.toFixed(3), debouncedBbox.north.toFixed(3),
      ].join(','),
    ],
    queryFn: ({ signal }) => fetchCompareMap(debouncedBbox!, kraje, 5000, signal),
    enabled: debouncedBbox != null,
    staleTime: 30_000,
  });

  const radiusQ = useQuery({
    queryKey: ['loc-compare', 'radius', krajeKey, centre?.lat, centre?.lng, radiusM],
    queryFn: ({ signal }) => fetchCompareRadius(centre!, radiusM, kraje, 100, signal),
    enabled: centre != null && radiusM >= 50 && radiusM <= 50_000,
    staleTime: 30_000,
  });

  const toggleKraj = useCallback((k: number) => {
    setKraje((prev) => {
      const next = prev.includes(k) ? prev.filter((x) => x !== k) : [...prev, k].sort();
      return next.length ? next : prev;
    });
    setOkresKod(null);
    setDetail(null);
    setObec(null);
  }, []);

  const okresy = useMemo(
    () => (scope.data?.okresy ?? []).filter((o) => kraje.includes(o.kraj_kod)),
    [scope.data, kraje],
  );

  const tableRows: UnitTableRow[] = useMemo(() => {
    if (okresKod == null) {
      return okresy.map((o) => ({
        code: o.okres_kod, name: o.name, n_old: o.n_old,
        n_new_certain: o.n_new_certain, n_new_possible: o.n_new_possible,
        n_only_old: o.n_only_old, n_only_new: o.n_only_new,
        agreement_pct: o.agreement_pct,
      }));
    }
    /* /compare/units publishes no agreement_pct; it is derivable from the same
     * definition the scope route uses — old-in-unit that the new engine also
     * shows is exactly n_old minus the only-old rows. */
    return (units.data?.rows ?? []).map((r) => ({
      ...r,
      agreement_pct: r.n_old ? (100 * (r.n_old - r.n_only_old)) / r.n_old : null,
    }));
  }, [okresKod, okresy, units.data]);

  const tableError = okresKod == null ? scope.error : units.error;
  const tableLoading = okresKod == null ? scope.isLoading : units.isLoading;

  const mapRows = useMemo<MapRow[]>(() => mapQ.data?.rows ?? [], [mapQ.data]);
  /* Counted server-side over the whole box: these rows sort last (delta_m is
   * NULL) and are the first the row cap drops, so the visible page under-counts
   * them exactly when there are most of them. */
  const noNewPosition = mapQ.data?.counts.only_old_geom ?? 0;

  const pickRow = useCallback((row: UnitTableRow) => {
    if (okresKod == null) {
      setOkresKod(row.code);
      setObec(null);
      setDetail({ level: 'okres', code: row.code });
    } else {
      setObec({ code: row.code, name: row.name });
      setDetail({ level: 'obec', code: row.code });
    }
  }, [okresKod]);

  const generatedAt = scope.data?.generated_at ?? null;

  return (
    <div className="px-6 pt-5 pb-10 max-w-screen-2xl mx-auto">
      <header>
        <h1 className="text-2xl leading-tight">Location compare</h1>
        <p className="text-sm text-[var(--color-ink-2)]">
          Old filters and map (what Browse shows today) against the new location engine,
          property grain, read-only. Nothing here changes what Browse serves.
        </p>
      </header>

      <div className="sticky top-14 z-20 -mx-6 mt-4 border-y border-[var(--color-rule)] bg-[var(--color-paper)]/95 px-6 py-2.5 backdrop-blur-sm">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <span className="text-[0.62rem] tracking-[0.14em] uppercase text-[var(--color-ink-3)]">Scope</span>
          {KRAJ_CHIPS.map((k) => (
            <PickButton key={k} on={kraje.includes(k)} onClick={() => toggleKraj(k)}>
              {KRAJ_LABELS[k]} ({k})
            </PickButton>
          ))}
          <OkresPicker
            options={okresy.map((o) => ({ code: o.okres_kod, name: o.name }))}
            value={okresKod}
            onChange={(code) => {
              setOkresKod(code);
              setObec(null);
              setDetail(code == null ? null : { level: 'okres', code });
            }}
          />
          <span className="ml-auto text-[0.68rem] text-[var(--color-ink-4)]">
            {generatedAt ? `generated ${generatedAt}` : 'generating…'}
          </span>
        </div>
        <p className="mt-1.5 text-[0.7rem] text-[var(--color-ink-3)]">
          <span className="text-[var(--color-sage)]">certain</span> — the new engine is sure the
          property is in this unit ·{' '}
          <span className="text-[var(--color-copper)]">possible</span> — it might be; shown, but
          badged ·{' '}
          <span className="text-[var(--color-ochre)]">no</span> — the new engine puts it somewhere
          else ·{' '}
          <span className="text-[var(--color-brick)]">no row</span> — the new engine has no answer
          for it yet.
        </p>
      </div>

      <div className="mt-4 space-y-4">
        <Card
          title={okresKod == null ? 'Filters — okresy' : 'Filters — obce in the picked okres'}
          accessory={okresKod != null ? (
            <button
              type="button"
              onClick={() => { setOkresKod(null); setObec(null); setDetail(null); }}
              className="text-[0.7rem] text-[var(--color-copper)] underline decoration-dotted"
            >
              back to all okresy
            </button>
          ) : undefined}
        >
          {tableError ? <ErrorBanner error={tableError} />
            : tableLoading ? <Skeleton rows={5} />
            : tableRows.length === 0 ? <Empty />
            : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className={HEAD}>
                      <th className={TH}>Unit</th>
                      <th className={`${TH} text-right`}>Old</th>
                      <th className={`${TH} text-right`}>New certain</th>
                      <th className={`${TH} text-right`}>New possible</th>
                      <th className={`${TH} text-right`}>Only old</th>
                      <th className={`${TH} text-right`}>Only new</th>
                      <th className={`${TH} text-right`}>Agreement</th>
                    </tr>
                  </thead>
                  <tbody>
                    {tableRows.map((r) => (
                      <tr
                        key={r.code}
                        onClick={() => pickRow(r)}
                        className={`cursor-pointer border-t border-[var(--color-rule-soft)] hover:bg-[var(--color-copper-soft)] ${detail?.code === r.code ? 'bg-[var(--color-copper-soft)]' : ''}`}
                      >
                        <td className="py-1.5 pr-3">
                          {r.name ?? r.code}{' '}
                          <span className="text-[0.68rem] text-[var(--color-ink-4)]">{r.code}</span>
                        </td>
                        <td className={NUM}>{fmtCount(r.n_old)}</td>
                        <td className={`${NUM} text-[var(--color-sage)]`}>{fmtCount(r.n_new_certain)}</td>
                        <td className={NUM}>
                          <Badge tone="copper">{fmtCount(r.n_new_possible)}</Badge>
                        </td>
                        <td className={`${NUM} text-[var(--color-ochre)]`}>{fmtCount(r.n_only_old)}</td>
                        <td className={`${NUM} text-[var(--color-brick)]`}>{fmtCount(r.n_only_new)}</td>
                        <td className={NUM}>{pct(r.agreement_pct)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          {detail ? <UnitDetail level={detail.level} code={detail.code} kraje={kraje} /> : null}
        </Card>

        {obec ? (
          <StreetSection
            key={obec.code}
            obecKod={obec.code}
            obecName={obec.name}
            kraje={kraje}
          />
        ) : null}

        <Card
          title="Map — old vs new"
          accessory={
            <div className="flex flex-wrap items-center gap-2 text-[0.68rem] text-[var(--color-ink-3)]">
              {mapQ.data?.truncated ? (
                <Badge tone="ochre">zoom in — more rows than the 5 000 cap</Badge>
              ) : null}
              {noNewPosition > 0 ? (
                <Badge tone="brick">
                  {fmtCount(noNewPosition)} with no new position (area chips at their admin unit)
                </Badge>
              ) : null}
            </div>
          }
        >
          <MapLegend />
          <div className="mt-3">
            <CompareMapPair
              rows={mapRows}
              hoveredId={hoveredId}
              onHover={setHoveredId}
              onBboxChange={setBbox}
              centre={centre}
              radiusM={radiusM}
              pickMode={pickMode}
              onPickCentre={(c) => { setCentre(c); setPickMode(false); }}
            />
          </div>
          {mapQ.error ? <div className="mt-3"><ErrorBanner error={mapQ.error} /></div> : null}
          {mapQ.data ? (
            <div className="mt-3 grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
              <Stat label="Both sides" value={fmtCount(mapQ.data.counts.both)} />
              <Stat label="Only old geom" value={fmtCount(mapQ.data.counts.only_old_geom)} tone="ochre" />
              <Stat label="Only new geom" value={fmtCount(mapQ.data.counts.only_new_geom)} tone="brick" />
              <Stat label="Moved > 100 m" value={fmtCount(mapQ.data.counts.moved_gt_100m)} tone="copper" />
              <Stat label="Demoted to circle" value={fmtCount(mapQ.data.counts.demoted_to_circle)} tone="copper" />
              <Stat label="No geom either (whole scope)" value={fmtCount(mapQ.data.counts.no_geom_either)} />
            </div>
          ) : null}
          <div className="mt-4">
            {mapQ.isLoading && !mapQ.data ? <Skeleton rows={4} />
              : <DeltaTable rows={mapRows} hoveredId={hoveredId} onHover={setHoveredId} />}
          </div>
        </Card>

        <Card title="Radius">
          <p className="text-[0.72rem] text-[var(--color-ink-4)]">
            The old side is a bounding BOX around the centre, not a circle — that is the legacy
            behaviour, so its corners pull in listings the circle never would. The new side is a
            true distance test that also spends each row's uncertainty radius.
          </p>
          <div className="mt-3 flex flex-wrap items-end gap-3">
            <PickButton on={pickMode} onClick={() => setPickMode((v) => !v)}>
              {pickMode ? 'click either map…' : 'pick centre on map'}
            </PickButton>
            <Field label="Radius (m)" as="control">
              <input
                type="number" min={50} max={50_000} step={50}
                className="w-28 rounded-[var(--radius-xs)] border border-[var(--color-rule)] bg-[var(--color-inset)] px-2 py-1.5 text-sm"
                value={radiusM}
                onChange={(e) => setRadiusM(Number(e.target.value))}
              />
            </Field>
            {centre ? (
              <span className="text-[0.7rem] text-[var(--color-ink-3)] font-mono">
                {centre.lat.toFixed(5)}, {centre.lng.toFixed(5)}
              </span>
            ) : (
              <span className="text-[0.7rem] text-[var(--color-ink-4)]">no centre picked yet</span>
            )}
          </div>
          {radiusM < 50 || radiusM > 50_000 ? (
            <p className="mt-2 text-[0.72rem] text-[var(--color-brick)]">
              Radius must be between 50 and 50 000 m.
            </p>
          ) : null}
          {radiusQ.error ? <div className="mt-3"><ErrorBanner error={radiusQ.error} /></div> : null}
          {centre == null ? (
            <p className="mt-3 text-sm text-[var(--color-ink-3)]">Pick a centre to compare.</p>
          ) : radiusQ.isLoading ? <div className="mt-3"><Skeleton rows={3} /></div>
            : radiusQ.data ? (
              <>
                <div className="mt-3 grid grid-cols-2 sm:grid-cols-3 gap-2">
                  <Stat label="Old bbox count" value={fmtCount(radiusQ.data.old_bbox_count)} tone="ochre" />
                  <Stat label="New certain" value={fmtCount(radiusQ.data.new_certain)} tone="sage" />
                  <Stat label="New possible" value={fmtCount(radiusQ.data.new_possible)} tone="copper" />
                </div>
                <div className="mt-4 grid gap-6 lg:grid-cols-2">
                  <div>
                    <div className="text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]">
                      In the old box, not in the new circle
                    </div>
                    <div className="mt-2 max-h-72 overflow-y-auto divide-y divide-[var(--color-rule-soft)]">
                      {radiusQ.data.only_old.length === 0 ? <Empty /> : radiusQ.data.only_old.map((r) => (
                        <div key={r.property_id} className="flex items-center gap-2 py-1.5 text-sm">
                          <Link
                            to={propertyListingPath(r.property_id)}
                            className="font-mono text-[0.75rem] text-[var(--color-copper)] underline decoration-dotted"
                          >
                            #{r.property_id}
                          </Link>
                          <span className="text-[var(--color-ink-3)]">{r.source ?? '—'}</span>
                          <span className="ml-auto font-mono tabular-nums text-[0.72rem]">
                            {r.delta_m == null ? '—' : `${Math.round(r.delta_m)} m`}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                  <div>
                    <div className="text-[0.65rem] tracking-[0.1em] uppercase text-[var(--color-ink-3)]">
                      In the new circle, not in the old box
                    </div>
                    <div className="mt-2 max-h-72 overflow-y-auto divide-y divide-[var(--color-rule-soft)]">
                      {radiusQ.data.only_new.length === 0 ? <Empty /> : radiusQ.data.only_new.map((r) => (
                        <div key={r.property_id} className="flex items-center gap-2 py-1.5 text-sm">
                          <Link
                            to={propertyListingPath(r.property_id)}
                            className="font-mono text-[0.75rem] text-[var(--color-copper)] underline decoration-dotted"
                          >
                            #{r.property_id}
                          </Link>
                          <span className="text-[var(--color-ink-3)]">{r.source ?? '—'}</span>
                          <Badge tone={r.verdict === 'certain' ? 'sage' : 'copper'}>{r.verdict}</Badge>
                          <span className="ml-auto font-mono tabular-nums text-[0.72rem]">
                            ±{r.uncertainty_radius_m == null ? '—' : `${r.uncertainty_radius_m} m`}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              </>
            ) : null}
        </Card>

        <Card title="Assignment methods and sources in scope">
          {scope.error ? <ErrorBanner error={scope.error} />
            : scope.isLoading ? <Skeleton rows={4} />
            : scope.data ? (
              <>
                <div className="grid gap-4 lg:grid-cols-2">
                  <MiniMethods rows={scope.data.by_method} />
                  <MiniSources rows={scope.data.by_source} />
                </div>
                <div className="mt-4 grid grid-cols-2 sm:grid-cols-4 gap-2">
                  {scope.data.kraje_rows.map((k) => (
                    <Stat
                      key={k.kraj_kod}
                      label={k.name ?? String(k.kraj_kod)}
                      value={pct(k.agreement_pct)}
                      hint={`${fmtCount(k.n_old)} old · ${fmtCount(k.n_only_old)} only old · ${fmtCount(k.n_only_new)} only new`}
                    />
                  ))}
                </div>
              </>
            ) : null}
        </Card>
      </div>
    </div>
  );
}
