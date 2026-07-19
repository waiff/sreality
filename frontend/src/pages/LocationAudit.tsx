import { useMemo, useState, useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';

import FilterChip from '@/components/FilterChip';
import InfiniteSentinel from '@/components/InfiniteSentinel';
import { useInfiniteList } from '@/lib/useInfiniteList';
import {
  getLocationAudit,
  getLocationAuditRaw,
  type LocationAuditRow,
} from '@/lib/api';
import { CATEGORY_MAIN_TABS } from '@/lib/categoryMainTabs';
import { filterById } from '@/lib/filterRegistry.generated';
import { portalShort, portalListingUrl } from '@/lib/portals';
import { fmtRelative, fmtCount } from '@/lib/format';
import {
  FIELD_GROUPS,
  GEOM_METHOD,
  STREET_METHOD,
  methodLabel,
  type FieldSpec,
} from '@/lib/locationAudit';

/* /location-audit — a per-listing inventory of every address / coordinate /
 * admin field, WITH how each was acquired, filterable by portal, type, active
 * state, and by whether any individual location field is populated or empty.
 * Read-only: nothing here changes the pipeline. Backed by
 * api/routes/location_audit.py; field glossary in lib/locationAudit.ts. */

const PAGE_SIZE = 50;

const SOURCE_TABS: ReadonlyArray<{ id: string; label: string }> = [
  { id: '', label: 'Vše' },
  ...(filterById('portals')?.enum_values ?? []).map((o) => ({
    id: String(o.value),
    label: o.label_cs,
  })),
];

const ACTIVE_TABS: ReadonlyArray<{ id: '' | 'active' | 'inactive'; label: string }> = [
  { id: '', label: 'Vše' },
  { id: 'active', label: 'Aktivní' },
  { id: 'inactive', label: 'Neaktivní' },
];

const DEDUP_TABS: ReadonlyArray<{ id: '' | 'reachable' | 'unreachable'; label: string }> = [
  { id: '', label: 'Vše' },
  { id: 'reachable', label: 'Dosažitelné dedupem' },
  { id: 'unreachable', label: 'Nedosažitelné (nikdy nekandiduje)' },
];

// The dedup pass a row qualifies for (from the arm booleans), for the per-card badge.
function dedupBadge(r: LocationAuditRow): { label: string; reachable: boolean; tip: string } {
  if (!r.dedup_reachable) {
    return {
      label: 'nedosažitelné pro dedup',
      reachable: false,
      tip: 'Nesplňuje podmínky žádné z cest (ulice+dispozice / geo+plocha / byt-geo) — nikdy se nestane kandidátem, nikdy se neporovná.',
    };
  }
  const paths: string[] = [];
  if (r.elig_street) paths.push('ulice+dispozice');
  if (r.elig_geo) paths.push('geo+plocha');
  if (r.elig_byt_geo) paths.push('byt-geo');
  return {
    label: `dedup: ${paths.join(' · ') || 'dosažitelné'}`,
    reachable: true,
    tip: 'Cesta(y), kterou engine tento listing dokáže zařadit mezi kandidáty. geo/byt-geo cesty jsou jen pro aktivní listingy.',
  };
}

const CATEGORY_LABEL: Record<string, string> = Object.fromEntries(
  CATEGORY_MAIN_TABS.filter((t) => t.id).map((t) => [t.id, t.label]),
);

type Presence = Record<string, 'has' | 'missing'>;

interface LocPage {
  rows: LocationAuditRow[];
  nextCursor?: unknown;
  total: number | null;
}

export default function LocationAudit() {
  const [source, setSource] = useState('');
  const [categoryMain, setCategoryMain] = useState('');
  const [active, setActive] = useState<'' | 'active' | 'inactive'>('');
  const [dedup, setDedup] = useState<'' | 'reachable' | 'unreachable'>('');
  const [presence, setPresence] = useState<Presence>({});
  const [presenceOpen, setPresenceOpen] = useState(false);
  const [rawFor, setRawFor] = useState<LocationAuditRow | null>(null);

  const hasKeys = useMemo(
    () => Object.keys(presence).filter((k) => presence[k] === 'has').sort(),
    [presence],
  );
  const missingKeys = useMemo(
    () => Object.keys(presence).filter((k) => presence[k] === 'missing').sort(),
    [presence],
  );

  const cyclePresence = (key: string) =>
    setPresence((prev) => {
      const cur = prev[key];
      const next = { ...prev };
      if (cur === undefined) next[key] = 'has';
      else if (cur === 'has') next[key] = 'missing';
      else delete next[key];
      return next;
    });

  const list = useInfiniteList<LocationAuditRow, LocPage>({
    queryKey: ['location-audit', source, categoryMain, active, dedup, hasKeys, missingKeys],
    queryFn: async (cursor) => {
      const offset = (cursor as number | null) ?? 0;
      const resp = await getLocationAudit({
        source: source || undefined,
        category_main: categoryMain || undefined,
        active: active || undefined,
        dedup: dedup || undefined,
        has: hasKeys.length ? hasKeys : undefined,
        missing: missingKeys.length ? missingKeys : undefined,
        limit: PAGE_SIZE,
        offset,
      });
      return {
        rows: resp.data,
        nextCursor: offset + resp.returned,
        total: resp.total,
      };
    },
    pageSize: PAGE_SIZE,
    getRowId: (r) => `${r.source}:${r.sreality_id}`,
  });

  const total = list.firstPage?.total ?? null;
  const activeFilters = hasKeys.length + missingKeys.length;

  return (
    <div className="px-6 py-8 max-w-5xl mx-auto">
      <header>
        <h1 className="text-2xl leading-tight">Location Audit</h1>
        <p className="mt-1 text-sm text-[var(--color-ink-2)] max-w-3xl">
          Kompletní přehled adresních, geo a souřadnicových polí každého listingu —
          s tím, jak byla každá hodnota získána. Filtruj podle portálu, typu, stavu a
          podle toho, které pole je (ne)vyplněné. Jen pro čtení.
        </p>
      </header>

      <Explainer />

      {/* Filter bar */}
      <div className="mt-6 flex flex-col gap-2">
        <FilterRow label="Zdroj">
          {SOURCE_TABS.map((t) => (
            <FilterChip key={t.id} on={source === t.id} label={t.label} onClick={() => setSource(t.id)} />
          ))}
        </FilterRow>
        <FilterRow label="Typ">
          {CATEGORY_MAIN_TABS.map((t) => (
            <FilterChip key={t.id} on={categoryMain === t.id} label={t.label} onClick={() => setCategoryMain(t.id)} />
          ))}
        </FilterRow>
        <FilterRow label="Stav">
          {ACTIVE_TABS.map((t) => (
            <FilterChip key={t.id} on={active === t.id} label={t.label} onClick={() => setActive(t.id)} />
          ))}
        </FilterRow>
        <FilterRow label="Dedup">
          {DEDUP_TABS.map((t) => (
            <FilterChip key={t.id} on={dedup === t.id} label={t.label} onClick={() => setDedup(t.id)} />
          ))}
        </FilterRow>
      </div>

      {/* Presence-by-field filter */}
      <div className="mt-3 border border-[var(--color-rule)] rounded-[var(--radius-sm)] bg-[var(--color-paper-2)]">
        <button
          type="button"
          onClick={() => setPresenceOpen((v) => !v)}
          className="w-full px-4 py-2.5 flex items-center justify-between gap-2 text-left text-sm"
        >
          <span>
            Filtr podle pole (vyplněno / prázdné)
            {activeFilters > 0 && (
              <span className="ml-2 text-[var(--color-copper)]">· {activeFilters} aktivní</span>
            )}
          </span>
          <span className="flex items-center gap-3">
            {activeFilters > 0 && (
              <span
                role="button"
                tabIndex={0}
                onClick={(e) => {
                  e.stopPropagation();
                  setPresence({});
                }}
                className="text-[0.72rem] text-[var(--color-ink-4)] hover:text-[var(--color-ink-2)]"
              >
                Vyčistit
              </span>
            )}
            <span className="text-[var(--color-ink-4)]" aria-hidden>{presenceOpen ? '▴' : '▾'}</span>
          </span>
        </button>
        {presenceOpen && (
          <div className="px-4 pb-4 pt-1 flex flex-col gap-3">
            <p className="text-[0.72rem] text-[var(--color-ink-4)]">
              Klikáním pole přepínáš: <span className="text-[var(--color-ink-3)]">vše → ✓ vyplněno → ∅ prázdné → vše</span>.
              Více polí se kombinuje (AND).
            </p>
            {FIELD_GROUPS.map((g) => (
              <div key={g.title} className="flex flex-col gap-1">
                <span className="text-[0.62rem] uppercase tracking-[0.1em] text-[var(--color-ink-4)]">{g.title}</span>
                <div className="flex flex-wrap gap-1.5">
                  {g.fields.filter((f) => f.presence).map((f) => (
                    <PresenceChip key={f.key} state={presence[f.key]} label={f.label} onClick={() => cyclePresence(f.key)} />
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <p className="mt-3 text-[0.72rem] text-[var(--color-ink-4)]">
        {total != null ? `${fmtCount(total)} listingů odpovídá filtru` : 'Načítám…'}
        {' · aktivní a naposledy viděné první'}
      </p>

      <div className="mt-4 flex flex-col gap-3">
        {list.isLoading ? (
          <p className="text-sm text-[var(--color-ink-3)]">Načítám…</p>
        ) : list.rows.length === 0 ? (
          <p className="text-sm text-[var(--color-ink-3)]">Žádné listingy neodpovídají filtru.</p>
        ) : (
          list.rows.map((r) => (
            <ListingCard key={`${r.source}:${r.sreality_id}`} row={r} onShowRaw={() => setRawFor(r)} />
          ))
        )}
      </div>

      <InfiniteSentinel
        onReach={list.fetchNextPage}
        hasNextPage={list.hasNextPage}
        isFetchingNextPage={list.isFetchingNextPage}
        loadedCount={list.loadedCount}
        total={total}
        loadingLabel="Načítám další listingy…"
      />

      {rawFor && <RawJsonModal row={rawFor} onClose={() => setRawFor(null)} />}
    </div>
  );
}

function FilterRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="text-[0.62rem] uppercase tracking-[0.1em] text-[var(--color-ink-4)] mr-1 w-12">
        {label}
      </span>
      {children}
    </div>
  );
}

function PresenceChip({
  state,
  label,
  onClick,
}: {
  state: 'has' | 'missing' | undefined;
  label: string;
  onClick: () => void;
}) {
  const cls =
    state === 'has'
      ? 'border-[var(--color-copper)] bg-[var(--color-copper-soft)] text-[var(--color-copper)]'
      : state === 'missing'
        ? 'border-[var(--color-rule-strong)] bg-[var(--color-paper)] text-[var(--color-ink-2)]'
        : 'border-[var(--color-rule)] text-[var(--color-ink-4)] hover:text-[var(--color-ink-2)]';
  const mark = state === 'has' ? '✓ ' : state === 'missing' ? '∅ ' : '';
  return (
    <button
      type="button"
      onClick={onClick}
      className={['px-2 py-0.5 rounded-[var(--radius-sm)] border text-[0.7rem] font-mono transition-colors', cls].join(' ')}
      title={state === 'has' ? 'vyplněno' : state === 'missing' ? 'prázdné' : 'bez omezení'}
    >
      {mark}{label}
    </button>
  );
}

function ListingCard({ row, onShowRaw }: { row: LocationAuditRow; onShowRaw: () => void }) {
  const portalUrl = portalListingUrl(row.source, row.source_url, row.sreality_id, {
    categoryType: row.category_type,
    categoryMain: row.category_main,
    categorySubCb: row.category_sub_cb,
  });
  const cat = row.category_main ? (CATEGORY_LABEL[row.category_main] ?? row.category_main) : null;
  const dedup = dedupBadge(row);

  return (
    <div className="border border-[var(--color-rule)] rounded-[var(--radius-sm)] bg-[var(--color-paper)]">
      {/* Header */}
      <div className="px-4 py-2.5 flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-[var(--color-rule)]">
        <span className="px-1.5 py-0.5 rounded-[var(--radius-xs)] bg-[var(--color-paper-2)] border border-[var(--color-rule)] text-[0.7rem] font-medium">
          {portalShort(row.source)}
        </span>
        {cat && <span className="text-[0.72rem] text-[var(--color-ink-3)]">{cat}</span>}
        <span className="font-mono text-[0.72rem] text-[var(--color-ink-4)]">#{row.sreality_id}</span>
        {row.source_id_native && row.source_id_native !== String(row.sreality_id) && (
          <span className="font-mono text-[0.68rem] text-[var(--color-ink-4)]" title="native id na portálu">
            ({row.source_id_native})
          </span>
        )}
        <span
          className={[
            'px-1.5 py-0.5 rounded-[var(--radius-xs)] text-[0.66rem]',
            row.is_active
              ? 'bg-[var(--color-copper-soft)] text-[var(--color-copper)]'
              : 'bg-[var(--color-paper-2)] text-[var(--color-ink-4)] border border-[var(--color-rule)]',
          ].join(' ')}
        >
          {row.is_active ? 'aktivní' : 'neaktivní'}
        </span>
        <span
          className={[
            'px-1.5 py-0.5 rounded-[var(--radius-xs)] text-[0.66rem] border cursor-help',
            dedup.reachable
              ? 'border-[var(--color-sage)] bg-[var(--color-sage-soft)] text-[var(--color-sage)]'
              : 'border-[var(--color-brick)] bg-[var(--color-brick-soft)] text-[var(--color-brick)]',
          ].join(' ')}
          title={dedup.tip}
        >
          {dedup.label}
        </span>
        {(row.obec || row.okres) && (
          <span className="text-[0.72rem] text-[var(--color-ink-3)]">
            {[row.obec, row.okres].filter(Boolean).join(', ')}
          </span>
        )}
        <span className="text-[0.68rem] text-[var(--color-ink-4)]">
          {row.is_active ? `viděno ${fmtRelative(row.last_seen_at)}` : `zneaktivněno ${fmtRelative(row.inactive_at)}`}
        </span>
        <span className="ml-auto flex items-center gap-2">
          {portalUrl ? (
            <a
              href={portalUrl}
              target="_blank"
              rel="noreferrer"
              className="text-[0.72rem] text-[var(--color-copper)] hover:underline"
            >
              Otevřít na portálu ↗
            </a>
          ) : (
            <span className="text-[0.72rem] text-[var(--color-ink-4)]" title="nelze sestavit externí URL">
              bez portál URL
            </span>
          )}
          <button
            type="button"
            onClick={onShowRaw}
            className="text-[0.72rem] px-2 py-0.5 rounded-[var(--radius-xs)] border border-[var(--color-rule)] text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)]"
          >
            Zdrojová data (JSON)
          </button>
        </span>
      </div>

      {/* Field grid */}
      <div className="px-4 py-3 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-x-6 gap-y-3">
        {FIELD_GROUPS.map((g) => (
          <div key={g.title} className="flex flex-col gap-1">
            <span
              className="text-[0.6rem] uppercase tracking-[0.1em] text-[var(--color-ink-4)]"
              title={g.hint}
            >
              {g.title}
            </span>
            <dl className="flex flex-col gap-0.5">
              {g.fields.map((f) => (
                <FieldRow key={f.key} field={f} row={row} />
              ))}
            </dl>
          </div>
        ))}
      </div>
    </div>
  );
}

function FieldRow({ field, row }: { field: FieldSpec; row: LocationAuditRow }) {
  const value = field.value(row);
  const present = value !== null && value !== '';
  const method =
    field.key === 'geom'
      ? methodLabel(GEOM_METHOD, row.geom_method)
      : field.key === 'street'
        ? methodLabel(STREET_METHOD, row.street_method)
        : null;

  return (
    <div className="grid grid-cols-[minmax(0,7.5rem)_1fr] gap-2 items-baseline text-[0.72rem]">
      <dt
        className="font-mono text-[0.66rem] text-[var(--color-ink-4)] truncate cursor-help"
        title={field.explain}
      >
        {field.label}
      </dt>
      <dd className={present ? 'text-[var(--color-ink)] break-words' : 'text-[var(--color-ink-4)]'}>
        {present ? String(value) : '—'}
        {method && (
          <span
            className="ml-1.5 inline-block px-1 py-px rounded-[var(--radius-xs)] bg-[var(--color-paper-2)] border border-[var(--color-rule)] text-[0.6rem] text-[var(--color-ink-3)] cursor-help align-middle"
            title={method.tip}
          >
            {method.label}
          </span>
        )}
      </dd>
    </div>
  );
}

function RawJsonModal({ row, onClose }: { row: LocationAuditRow; onClose: () => void }) {
  const q = useQuery({
    queryKey: ['location-audit', 'raw', row.source, row.sreality_id],
    queryFn: () => getLocationAuditRaw(row.sreality_id),
    staleTime: 60_000,
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const pretty = q.data ? JSON.stringify(q.data.raw_json, null, 2) : '';

  const copy = () => {
    if (pretty) void navigator.clipboard?.writeText(pretty);
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-[var(--color-ink)]/40 px-4 pt-[8vh]"
      onClick={onClose}
      role="presentation"
    >
      <div
        className="w-full max-w-3xl max-h-[82vh] flex flex-col rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper)] shadow-lg"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Zdrojová data listingu"
      >
        <div className="px-5 py-3 flex items-center gap-3 border-b border-[var(--color-rule)]">
          <span className="px-1.5 py-0.5 rounded-[var(--radius-xs)] bg-[var(--color-paper-2)] border border-[var(--color-rule)] text-[0.7rem] font-medium">
            {portalShort(row.source)}
          </span>
          <span className="font-mono text-[0.74rem] text-[var(--color-ink-3)]">#{row.sreality_id}</span>
          <span className="text-[0.74rem] text-[var(--color-ink-4)]">raw_json (původní zachycený payload)</span>
          <span className="ml-auto flex items-center gap-2">
            <button
              type="button"
              onClick={copy}
              disabled={!pretty}
              className="text-[0.72rem] px-2 py-0.5 rounded-[var(--radius-xs)] border border-[var(--color-rule)] text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)] disabled:opacity-40"
            >
              Kopírovat
            </button>
            <button
              type="button"
              onClick={onClose}
              className="text-[0.72rem] px-2 py-0.5 rounded-[var(--radius-xs)] border border-[var(--color-rule)] text-[var(--color-ink-2)] hover:border-[var(--color-rule-strong)]"
            >
              Zavřít
            </button>
          </span>
        </div>
        <div className="overflow-auto p-4">
          {q.isLoading ? (
            <p className="text-sm text-[var(--color-ink-3)]">Načítám…</p>
          ) : q.isError ? (
            <p className="text-sm text-[var(--color-ink-3)]">Nepodařilo se načíst zdrojová data.</p>
          ) : (
            <pre className="text-[0.72rem] leading-relaxed font-mono text-[var(--color-ink)] whitespace-pre-wrap break-words">
              {pretty}
            </pre>
          )}
        </div>
      </div>
    </div>
  );
}

function Explainer() {
  const [open, setOpen] = useState(false);
  return (
    <div className="mt-4 border border-[var(--color-rule)] rounded-[var(--radius-sm)] bg-[var(--color-paper-2)]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full px-4 py-2.5 flex items-center justify-between gap-2 text-left text-sm"
      >
        <span>Jak vznikají location data (glosář polí)</span>
        <span className="text-[var(--color-ink-4)]" aria-hidden>{open ? '▴' : '▾'}</span>
      </button>
      {open && (
        <div className="px-4 pb-4 text-[0.82rem] text-[var(--color-ink-2)] leading-relaxed space-y-3">
          <p>
            <strong className="text-[var(--color-ink)]">Kotva je souřadnice.</strong> Portál dodá
            GPS pin (nativní), nebo se souřadnice geokóduje z textu (Mapy.cz), případně přenese z
            minula. Z <span className="font-mono">geom</span> se pak triggerem odvodí celá admin
            hierarchie (obec/okres/kraj) — jednotně napříč všemi 9 portály, nezávisle na volném textu.
          </p>
          <p>
            <strong className="text-[var(--color-ink)]">Ulice</strong> je buď strukturované pole
            portálu (sreality, bezrealitky, mmreality), nebo se vytěží z volného textu (idnes, remax,
            realitymix, maxima, ceskereality, bazos), nebo ji doplní RÚIAN coord→street resolver z
            uložených souřadnic (<span className="font-mono">street_source='resolver'</span>).
          </p>
          <p>
            Značky u <span className="font-mono">geom</span> a <span className="font-mono">street</span> ukazují
            metodu pro daný řádek; u ostatních polí najedeš myší na název pole a zobrazí se, jak
            vzniká. Sekce „Signály přesnosti" ukazuje portálem deklarované příznaky
            (<span className="font-mono">inaccuracy_type</span>, <span className="font-mono">accurate</span>,
            <span className="font-mono"> coords.source</span>), které pipeline zatím z velké části nečte —
            přesně to, co tu jde hledat a odemknout.
          </p>
          <p>
            <strong className="text-[var(--color-ink)]">Dedup dosažitelnost.</strong> Engine zařadí
            listing mezi kandidáty jen přes jednu ze tří cest: <span className="font-mono">ulice+dispozice</span> (byt),
            <span className="font-mono"> geo+plocha</span> (dům/pozemek/komerce = kategorie + souřadnice + obec_id + plocha) nebo
            <span className="font-mono"> byt-geo</span> (byt bez ulice, ale se souřadnicí + plochou + dispozicí). Listing, který
            nesplní <em>žádnou</em>, se nikdy neporovná — tady ho vyfiltruješ přes „Nedosažitelné". Predikát je přesně
            engineova vlastní podmínka způsobilosti (<span className="font-mono">toolkit.publication.eligible_predicate</span>,
            hlídaná parity testem). geo a byt-geo cesty platí jen pro aktivní listingy (proto může být
            neaktivní řádek „nedosažitelný", i když má data).
          </p>
        </div>
      )}
    </div>
  );
}
