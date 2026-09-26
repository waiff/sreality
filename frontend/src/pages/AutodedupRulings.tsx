/* AUTODEDUP · Rozhodnutí — every operator ruling, beside the engine's view (E919).
 *
 * No other surface lists the rulings: a queue shows one only while its pair or
 * group sits in the chosen generation's queue. Here every ruling is a row, newest
 * first (`GET /autodedup/rulings`): pair rulings typed against the pair, the
 * Browse merges' `same`, the member pairs a confirmed group implies (shown as
 * such — the lane never binds them), bare operator vetoes, and at group grain
 * the group rulings and the Browse merges as groups.
 *
 * Each row puts the ruling against production (are the adverts on one property
 * now?) and the engine (the live stream's grouping, the stored pair's zone,
 * score and certificate). "Neshody" is the first chip: the rulings the state of
 * things contradicts — the answer to "were my decisions right, and are they
 * obeyed?".
 *
 * A CORRECTION IS A NEW RULING, NEVER A DELETE (migration 573). Flip and
 * Withdraw post `POST /autodedup/verdict` with `supersedes` — the ruling the row
 * showed; the server answers 409 when it was ruled again since this page loaded.
 * A withdrawal is a newer `unsure`. The lane reads the newest word on its next
 * pass. It never splits a property (decision 9) and merges only inside its
 * scope, so where a ruling and a property disagree the row offers the existing
 * split (`POST /properties/{id}/detach`) or merge (`POST /properties/merge`),
 * each behind a second click. */

import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import ErrorBanner from '@/components/ErrorBanner';
import Spinner from '@/components/Spinner';
import MemberGrid from '@/components/autodedup/MemberGrid';
import { PHOTOS_PER_ADVERT, memberFromListing } from '@/components/autodedup/memberFromListing';
import { pairHref } from '@/components/autodedup/filterState';
import {
  NEGATIVE_VERDICTS,
  VERDICT_LABELS,
  displayVerdict,
} from '@/components/autodedup/VerdictButtons';
import {
  ApiError,
  DETACH_REASON_MAX,
  detachListing,
  getAutodedupRulings,
  mergePropertySet,
  postAutodedupVerdict,
  type AutodedupMember,
  type AutodedupVerdictInput,
  type AutodedupVerdictRow,
  type AutodedupVerdictValue,
  type RulingAgreement,
  type RulingFilters,
  type RulingGroupRow,
  type RulingPairRow,
  type RulingStatus,
  type RulingTown,
  type RulingsPage,
} from '@/lib/api';
import { fmtAbsolute, fmtCount } from '@/lib/format';
import { useListingPhotos } from '@/lib/hydration/useCardHydration';
import { propertyPath } from '@/lib/listingUrl';
import { detachOutcomeNote, inzeratu, mergedAdvertsKeys, refreshAfterDetach } from '@/lib/mergedAdverts';
import { fetchListingsForListingIds } from '@/lib/queries';
import { ROUTES, withQuery } from '@/lib/routes';
import type { ListingPublic } from '@/lib/types';
import { useUrlFilters } from '@/lib/useUrlFilters';

const PAGE_SIZE = 25;
const NOTE_MAX = 2000;
/* The rulings payload names adverts by id only; the card's portal arrives with the facts read. */
const NO_FALLBACK = { source: null, is_active: null };

/* ------------------------------------------------------------ the filter state */

export interface RulingFilterState {
  grain: string;
  engine: string;
  verdict: string;
  source: string;
  status: string;
  now: string;
  town: string;
  decided_from: string;
  decided_to: string;
  listing: string;
  property: string;
  merge_group: string;
  generation: string;
}

export const RULING_DEFAULTS: RulingFilterState = {
  grain: 'pair',
  engine: '',
  verdict: '',
  source: '',
  status: '',
  now: '',
  town: '',
  decided_from: '',
  decided_to: '',
  listing: '',
  property: '',
  merge_group: '',
  generation: '',
};

const SOURCES: Record<'pair' | 'group', ReadonlyArray<string>> = {
  pair: ['pair', 'browse_merge', 'implied', 'must_not_link'],
  group: ['group', 'browse_merge'],
};

export const SOURCE_LABEL: Record<string, string> = {
  pair: 'k páru',
  browse_merge: 'sloučení v Browse',
  implied: 'ze skupiny (implikováno)',
  must_not_link: 'zákaz spojení',
  group: 'skupina',
};

export const STATUS_LABEL: Record<RulingStatus, string> = {
  standing: 'platí',
  withdrawn: 'odvoláno',
  unsure: 'nevím',
};

const AGREEMENT_LABEL: Record<RulingAgreement, string> = {
  agrees: 'souhlasí',
  disagrees: 'neshoda',
  none: 'bez stanoviska',
};

const VERDICT_FILTERS = ['same', 'different', 'unsure'] as const;
const DIGITS = /^\d{1,19}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const TOWN = /^[oc]:\d{1,12}$/;
const DAY = /^\d{4}-\d{2}-\d{2}$/;

/* A hand-edited or stale link shows the list rather than a red banner: a value
 * outside a vocabulary is no filter, as on the review queues. */
export function sanitizeRulingFilters(raw: RulingFilterState): RulingFilterState {
  const grain = raw.grain === 'group' ? 'group' : 'pair';
  const pick = (value: string, allowed: ReadonlyArray<string>) =>
    allowed.includes(value) ? value : '';
  return {
    ...raw,
    grain,
    engine: pick(raw.engine, ['agrees', 'disagrees', 'none']),
    verdict: pick(raw.verdict, VERDICT_FILTERS),
    source: pick(raw.source, SOURCES[grain]),
    status: pick(raw.status, ['standing', 'withdrawn', 'unsure']),
    now: pick(raw.now, ['together', 'apart']),
    town: TOWN.test(raw.town) ? raw.town : '',
    decided_from: DAY.test(raw.decided_from) ? raw.decided_from : '',
    decided_to: DAY.test(raw.decided_to) ? raw.decided_to : '',
    listing: DIGITS.test(raw.listing) ? raw.listing : '',
    property: DIGITS.test(raw.property) ? raw.property : '',
    merge_group: UUID.test(raw.merge_group) ? raw.merge_group : '',
  };
}

function toQuery(f: RulingFilterState, after: string | null): RulingFilters {
  return {
    grain: f.grain === 'group' ? 'group' : 'pair',
    engine: f.engine || null,
    verdict: f.verdict || null,
    source: f.source || null,
    status: f.status || null,
    now: f.now || null,
    town: f.town || null,
    decided_from: f.decided_from || null,
    decided_to: f.decided_to || null,
    listing: f.listing ? Number(f.listing) : null,
    property: f.property ? Number(f.property) : null,
    merge_group: f.merge_group || null,
    generation: f.generation || null,
    after,
    limit: PAGE_SIZE,
  };
}

/* The link other surfaces use to open this page on one advert, property or merge. */
export function rulingsHref(query: Partial<RulingFilterState>) {
  return withQuery(ROUTES.autodedupRulings.build(), query);
}

/* ------------------------------------------------------------------ the page */

export default function AutodedupRulings() {
  const [rawFilters, setFilters] = useUrlFilters<RulingFilterState>(RULING_DEFAULTS);
  const filters = useMemo(() => sanitizeRulingFilters(rawFilters), [rawFilters]);
  const grain = filters.grain === 'group' ? 'group' : 'pair';

  /* The keyset cursor is not in the URL (a link means "this filter"), and a
   * filter change starts again at page one. */
  const filterKey = JSON.stringify(filters);
  const [paging, setPaging] = useState<{ key: string; cursors: Array<string | null> }>({
    key: filterKey,
    cursors: [null],
  });
  const cursors = paging.key === filterKey ? paging.cursors : [null];
  const after = cursors[cursors.length - 1];

  const q = useQuery({
    queryKey: ['autodedup', 'rulings', filterKey, after],
    queryFn: () => getAutodedupRulings(toQuery(filters, after)),
    staleTime: 15_000,
  });
  const page = (q.data?.data ?? null) as RulingsPage<RulingPairRow | RulingGroupRow> | null;
  const items = useMemo(() => page?.items ?? [], [page]);

  /* Every advert on the page in one facts read and one photo read. */
  const ids = useMemo(() => {
    const out = new Set<number>();
    for (const item of items) {
      if ('member_ids' in item) item.member_ids.forEach((id) => out.add(id));
      else {
        out.add(item.listing_lo);
        out.add(item.listing_hi);
      }
    }
    return [...out].sort((a, b) => a - b);
  }, [items]);
  const detailsQ = useQuery<Map<number, ListingPublic>, Error>({
    queryKey: mergedAdvertsKeys.listings(ids),
    queryFn: () => fetchListingsForListingIds(ids),
    enabled: ids.length > 0,
    staleTime: 60_000,
  });
  const { photos } = useListingPhotos(ids, PHOTOS_PER_ADVERT);
  const member = (id: number) =>
    memberFromListing(id, NO_FALLBACK, detailsQ.data?.get(id), photos.get(id) ?? []);

  const set = (patch: Partial<RulingFilterState>) => setFilters({ ...filters, ...patch });

  return (
    <div className="px-6 pt-5 pb-10 max-w-screen-xl mx-auto">
      <header>
        <h1 className="text-2xl leading-tight">AUTODEDUP · Rozhodnutí</h1>
        <p className="mt-1 text-sm text-[var(--color-ink-2)] leading-relaxed max-w-[56rem]">
          Všechna vaše rozhodnutí — k páru, ze sloučení v Browse, ze skupin i zákazy spojení — a
          vedle každého, co s inzeráty dělá engine a kde jsou teď. Oprava je vždy nové rozhodnutí:
          původní zůstává v historii a engine se při dalším průchodu řídí tím nejnovějším.
        </p>
        {page && (
          <p className="mt-2 text-[0.75rem] text-[var(--color-ink-3)] tabular-nums">
            engine: generace {page.generation ?? '—'} · {fmtCount(page.total)} rozhodnutí
          </p>
        )}
      </header>

      <FilterStrip
        filters={filters}
        grain={grain}
        page={page}
        onChange={set}
        onReset={() => setFilters({ ...RULING_DEFAULTS, grain })}
      />

      {q.error && <ErrorBanner message={(q.error as Error).message} />}
      {detailsQ.isError && <ErrorBanner message={`Údaje inzerátů: ${detailsQ.error.message}`} />}
      {q.isLoading && (
        <p className="mt-6 flex items-center gap-2 text-sm text-[var(--color-ink-3)]">
          <Spinner /> Načítám rozhodnutí…
        </p>
      )}
      {q.data && !q.data.store_ready && (
        <p className="mt-6 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4 text-sm text-[var(--color-ink-2)]">
          Úložiště programu v této databázi zatím není.
        </p>
      )}
      {page && items.length === 0 && (
        <p className="mt-6 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4 text-sm text-[var(--color-ink-2)]">
          Žádné rozhodnutí neodpovídá filtru.
        </p>
      )}

      <ul className="mt-4 space-y-5">
        {items.map((item) =>
          'member_ids' in item ? (
            <GroupRulingCard
              key={item.ruling_key}
              row={item}
              members={item.member_ids.map(member)}
              onPairs={(mergeGroup) =>
                setFilters({ ...RULING_DEFAULTS, grain: 'pair', merge_group: mergeGroup })
              }
            />
          ) : (
            <PairRulingCard
              key={`${item.listing_lo}-${item.listing_hi}`}
              row={item}
              generation={page?.generation ?? ''}
              lo={member(item.listing_lo)}
              hi={member(item.listing_hi)}
            />
          ),
        )}
      </ul>

      {page && (cursors.length > 1 || page.next_after != null) && (
        <div className="mt-6 flex items-center gap-3 text-sm">
          <button
            type="button"
            disabled={cursors.length <= 1}
            onClick={() => setPaging({ key: filterKey, cursors: cursors.slice(0, -1) })}
            className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-3 py-1 disabled:opacity-40"
          >
            ← Předchozí
          </button>
          <button
            type="button"
            disabled={page.next_after == null}
            onClick={() => setPaging({ key: filterKey, cursors: [...cursors, page.next_after] })}
            className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-3 py-1 disabled:opacity-40"
          >
            Další →
          </button>
        </div>
      )}
    </div>
  );
}

/* ---------------------------------------------------------------- the filters */

const CHIP =
  'rounded-[var(--radius-sm)] border px-2.5 py-1 text-[0.75rem] transition-colors tabular-nums';
const CHIP_ON = 'border-[var(--color-ink-2)] bg-[var(--color-paper-2)] text-[var(--color-ink)]';
const CHIP_OFF =
  'border-[var(--color-rule)] text-[var(--color-ink-3)] hover:text-[var(--color-ink)]';
const SELECT =
  'rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-2 py-1 text-[0.78rem] text-[var(--color-ink)]';
const LABEL = 'flex flex-col gap-0.5 text-[0.66rem] uppercase tracking-[0.08em] text-[var(--color-ink-3)]';

function counted(label: string, n: number | undefined): string {
  return n == null ? label : `${label} (${fmtCount(n)})`;
}

function FilterStrip({
  filters,
  grain,
  page,
  onChange,
  onReset,
}: {
  filters: RulingFilterState;
  grain: 'pair' | 'group';
  page: RulingsPage<unknown> | null;
  onChange: (patch: Partial<RulingFilterState>) => void;
  onReset: () => void;
}) {
  const facets = page?.facets;
  const towns = page?.towns ?? [];
  const pinned = [
    filters.listing && { key: 'listing' as const, label: `inzerát #${filters.listing}` },
    filters.property && { key: 'property' as const, label: `nemovitost #${filters.property}` },
    filters.merge_group && {
      key: 'merge_group' as const,
      label: `sloučení ${filters.merge_group.slice(0, 8)}`,
    },
  ].filter(Boolean) as Array<{ key: 'listing' | 'property' | 'merge_group'; label: string }>;

  return (
    <section aria-label="Filtry" className="mt-5 space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <div role="tablist" aria-label="Zrnitost" className="flex gap-1">
          {(['pair', 'group'] as const).map((g) => (
            <button
              key={g}
              role="tab"
              type="button"
              aria-selected={grain === g}
              onClick={() => onChange({ grain: g, source: '' })}
              className={`${CHIP} ${grain === g ? CHIP_ON : CHIP_OFF}`}
            >
              {g === 'pair' ? 'Páry' : 'Skupiny'}
            </button>
          ))}
        </div>
        <span className="mx-1 h-4 w-px bg-[var(--color-rule)]" aria-hidden />
        {/* "Neshody" first: the rulings the state of things contradicts. */}
        {([
          ['disagrees', 'Neshody'],
          ['', 'Vše'],
          ['agrees', 'Souhlasí'],
        ] as const).map(([value, label]) => (
          <button
            key={label}
            type="button"
            aria-pressed={filters.engine === value}
            onClick={() => onChange({ engine: value })}
            className={`${CHIP} ${filters.engine === value ? CHIP_ON : CHIP_OFF} ${
              value === 'disagrees' ? 'font-medium' : ''
            }`}
          >
            {value ? counted(label, facets?.engine?.[value]) : label}
          </button>
        ))}
        {pinned.map((p) => (
          <button
            key={p.key}
            type="button"
            onClick={() => onChange({ [p.key]: '' })}
            aria-label={`Zrušit filtr ${p.label}`}
            className={`${CHIP} ${CHIP_ON}`}
          >
            {p.label} ✕
          </button>
        ))}
      </div>

      <div className="flex flex-wrap items-end gap-3">
        <label className={LABEL}>
          Rozhodnutí
          <select
            className={SELECT}
            value={filters.verdict}
            onChange={(e) => onChange({ verdict: e.target.value })}
          >
            <option value="">vše</option>
            {VERDICT_FILTERS.map((v) => (
              <option key={v} value={v}>
                {counted(VERDICT_LABELS[v], facets?.verdict?.[v])}
              </option>
            ))}
          </select>
        </label>
        <label className={LABEL}>
          Původ
          <select
            className={SELECT}
            value={filters.source}
            onChange={(e) => onChange({ source: e.target.value })}
          >
            <option value="">vše</option>
            {SOURCES[grain].map((s) => (
              <option key={s} value={s}>
                {counted(SOURCE_LABEL[s], facets?.source?.[s])}
              </option>
            ))}
          </select>
        </label>
        <label className={LABEL}>
          Stav
          <select
            className={SELECT}
            value={filters.status}
            onChange={(e) => onChange({ status: e.target.value })}
          >
            <option value="">vše</option>
            {(['standing', 'withdrawn', 'unsure'] as const).map((s) => (
              <option key={s} value={s}>
                {counted(STATUS_LABEL[s], facets?.status?.[s])}
              </option>
            ))}
          </select>
        </label>
        <label className={LABEL}>
          Nyní
          <select
            className={SELECT}
            value={filters.now}
            onChange={(e) => onChange({ now: e.target.value })}
          >
            <option value="">vše</option>
            <option value="together">v jedné nemovitosti</option>
            <option value="apart">v různých nemovitostech</option>
          </select>
        </label>
        <label className={LABEL}>
          Obec / část
          <select
            className={SELECT}
            value={filters.town}
            onChange={(e) => onChange({ town: e.target.value })}
          >
            <option value="">všude</option>
            <TownOptions towns={towns} grain="o" label="Obce" />
            <TownOptions towns={towns} grain="c" label="Části obce" />
            {filters.town && !towns.some((t) => `${t.grain}:${t.code}` === filters.town) && (
              <option value={filters.town}>{filters.town}</option>
            )}
          </select>
        </label>
        <label className={LABEL}>
          Od
          <input
            type="date"
            className={SELECT}
            value={filters.decided_from}
            onChange={(e) => onChange({ decided_from: e.target.value })}
          />
        </label>
        <label className={LABEL}>
          Do (bez)
          <input
            type="date"
            className={SELECT}
            value={filters.decided_to}
            onChange={(e) => onChange({ decided_to: e.target.value })}
          />
        </label>
        <button
          type="button"
          onClick={onReset}
          className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-2.5 py-1 text-[0.75rem] text-[var(--color-ink-3)] hover:text-[var(--color-ink)]"
        >
          Zrušit filtry
        </button>
      </div>
    </section>
  );
}

function TownOptions({
  towns,
  grain,
  label,
}: {
  towns: RulingTown[];
  grain: 'o' | 'c';
  label: string;
}) {
  const shown = towns.filter((t) => t.grain === grain);
  if (shown.length === 0) return null;
  return (
    <optgroup label={label}>
      {shown.map((t) => (
        <option key={`${t.grain}:${t.code}`} value={`${t.grain}:${t.code}`}>
          {counted(t.name ?? String(t.code), t.n)}
        </option>
      ))}
    </optgroup>
  );
}

/* ------------------------------------------------------------ one ruling's head */

const TONE: Record<'same' | 'different' | 'unsure', string> = {
  same: 'border-[var(--color-sage)] text-[var(--color-sage)] bg-[var(--color-sage-soft)]',
  different: 'border-[var(--color-brick)] text-[var(--color-brick)] bg-[var(--color-brick-soft)]',
  unsure: 'border-[var(--color-rule-strong)] text-[var(--color-ink-3)]',
};
const BADGE = 'rounded-[var(--radius-xs)] border px-1.5 py-0.5 text-[0.66rem] uppercase tracking-[0.08em]';

function RulingHead({
  verdict,
  status,
  source,
  agreement,
  decidedBy,
  decidedAt,
  note,
  reasons,
  children,
}: {
  verdict: AutodedupVerdictValue;
  status: RulingStatus;
  source: string;
  agreement: RulingAgreement;
  decidedBy: string;
  decidedAt: string;
  note: string | null;
  reasons: string[];
  children?: React.ReactNode;
}) {
  const shown = displayVerdict(verdict);
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
      <span className={`${BADGE} ${TONE[shown]}`}>{VERDICT_LABELS[shown]}</span>
      <span className={`${BADGE} border-[var(--color-rule)] text-[var(--color-ink-2)]`}>
        {STATUS_LABEL[status]}
      </span>
      <span className="text-[0.72rem] text-[var(--color-ink-3)]">{SOURCE_LABEL[source] ?? source}</span>
      {agreement === 'disagrees' && (
        <span className={`${BADGE} border-[var(--color-brick)] text-[var(--color-brick)]`}>
          {AGREEMENT_LABEL.disagrees}
        </span>
      )}
      <span className="text-[0.72rem] text-[var(--color-ink-3)] tabular-nums">
        {decidedBy} · {fmtAbsolute(decidedAt)}
      </span>
      {children}
      {(note || reasons.length > 0) && (
        <p className="basis-full text-[0.75rem] text-[var(--color-ink-2)]">
          {note}
          {reasons.length > 0 && (
            <span className="text-[var(--color-ink-3)]">
              {note ? ' · ' : ''}
              {reasons.join(', ')}
            </span>
          )}
        </p>
      )}
    </div>
  );
}

function History({ rows }: { rows: AutodedupVerdictRow[] }) {
  const [open, setOpen] = useState(false);
  if (rows.length === 0) return null;
  return (
    <div className="mt-2">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="text-[0.72rem] text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
      >
        Historie ({fmtCount(rows.length)})
      </button>
      {open && (
        <ol className="mt-1 space-y-0.5 text-[0.72rem] text-[var(--color-ink-2)]">
          {[...rows].reverse().map((v, i) => (
            <li key={v.id ?? i} className="tabular-nums">
              {fmtAbsolute(v.decided_at)} · {VERDICT_LABELS[displayVerdict(v.verdict)]} ·{' '}
              {v.decided_by}
              {v.generation ? ` · ${v.generation}` : ''}
              {v.note ? ` · ${v.note}` : ''}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

/* ------------------------------------------------------------ the correction */

interface Correction {
  label: string;
  verdict: 'same' | 'different' | 'unsure';
  /* What the second click is asked to confirm. */
  explain: string;
}

/* What a ruling may become. A standing word flips or is withdrawn; a withdrawn or
 * `unsure` one is said again. */
export function corrections(
  verdict: AutodedupVerdictValue,
  status: RulingStatus,
  scope: 'pair' | 'group',
): Correction[] {
  const about = scope === 'pair' ? 'těchto dvou inzerátech' : 'celé této skupině';
  const same: Correction = {
    label: status === 'standing' ? 'Otočit na Stejné' : 'Stejné',
    verdict: 'same',
    explain:
      scope === 'pair'
        ? `Zapíše se nové rozhodnutí „Stejné“ o ${about} a zákaz spojení se zruší. Engine je při dalším průchodu spojí, jsou-li v oblasti, kterou zpracovává.`
        : `Zapíše se nové rozhodnutí „Stejné“ o ${about} a zruší se zákazy spojení mezi jejími inzeráty.`,
  };
  const different: Correction = {
    label: status === 'standing' ? 'Otočit na Různé' : 'Různé',
    verdict: 'different',
    explain:
      scope === 'pair'
        ? `Zapíše se nové rozhodnutí „Různé“ o ${about} a trvalý zákaz jejich spojení.`
        : `Zapíše se nové rozhodnutí „Různé“ o ${about}: engine ji nesloučí jako celek.`,
  };
  const withdraw: Correction = {
    label: 'Odvolat',
    verdict: 'unsure',
    explain: `Rozhodnutí o ${about} se odvolá: zapíše se nové „Nevím“ a engine se jím přestane řídit${
      scope === 'pair' ? ' (zákaz spojení se zruší)' : ''
    }.`,
  };
  if (status !== 'standing') return [same, different];
  return NEGATIVE_VERDICTS.includes(verdict) ? [same, withdraw] : [different, withdraw];
}

function CorrectionBar({
  options,
  build,
  disabledReason,
}: {
  options: Correction[];
  build: (verdict: Correction['verdict'], note: string | null) => AutodedupVerdictInput;
  disabledReason?: string | null;
}) {
  const qc = useQueryClient();
  const [armed, setArmed] = useState<Correction | null>(null);
  const [note, setNote] = useState('');
  const write = useMutation({
    mutationFn: (c: Correction) => postAutodedupVerdict(build(c.verdict, note.trim() || null)),
    onSuccess: () => {
      setArmed(null);
      setNote('');
    },
    /* Stale or not, the list re-reads: a 409 means someone ruled since. */
    onSettled: () => qc.invalidateQueries({ queryKey: ['autodedup'] }),
  });
  const conflict = write.error instanceof ApiError && write.error.status === 409;

  if (disabledReason) {
    return <p className="mt-3 text-[0.72rem] text-[var(--color-ink-4)]">{disabledReason}</p>;
  }
  return (
    <div className="mt-3">
      <div className="flex flex-wrap items-center gap-2">
        {options.map((o) => (
          <button
            key={o.verdict}
            type="button"
            disabled={write.isPending}
            aria-pressed={armed?.verdict === o.verdict}
            onClick={() => {
              write.reset();
              setArmed(o);
            }}
            className={`rounded-[var(--radius-sm)] border px-3 py-1 text-[0.78rem] transition-colors disabled:opacity-50 ${
              o.verdict === 'same'
                ? 'border-[var(--color-sage)] text-[var(--color-sage)] hover:bg-[var(--color-sage-soft)]'
                : o.verdict === 'different'
                  ? 'border-[var(--color-brick)] text-[var(--color-brick)] hover:bg-[var(--color-brick-soft)]'
                  : 'border-[var(--color-rule-strong)] text-[var(--color-ink-2)] hover:bg-[var(--color-paper)]'
            }`}
          >
            {o.label}
          </button>
        ))}
      </div>
      {armed && (
        <div
          role="group"
          aria-label="Potvrdit opravu"
          className="mt-2 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-3 py-2"
        >
          <p className="text-[0.78rem] text-[var(--color-ink-2)]">
            {armed.explain} Nic se nemaže — původní rozhodnutí zůstane v historii.
          </p>
          <textarea
            aria-label="Poznámka k opravě (nepovinné)"
            placeholder="Poznámka (nepovinné)"
            maxLength={NOTE_MAX}
            rows={2}
            value={note}
            disabled={write.isPending}
            onChange={(e) => setNote(e.target.value)}
            className="mt-2 block w-full max-w-[36rem] rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-2 py-1 text-[0.78rem] text-[var(--color-ink)]"
          />
          <div className="mt-2 flex items-center gap-2">
            <button
              type="button"
              autoFocus
              disabled={write.isPending}
              onClick={() => write.mutate(armed)}
              className="rounded-[var(--radius-sm)] border border-[var(--color-ink-2)] px-3 py-1 text-[0.78rem] text-[var(--color-ink)] hover:bg-[var(--color-paper-2)] disabled:opacity-50"
            >
              {write.isPending ? 'Zapisuji…' : 'Ano, zapsat'}
            </button>
            <button
              type="button"
              disabled={write.isPending}
              onClick={() => setArmed(null)}
              className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-3 py-1 text-[0.78rem] text-[var(--color-ink-2)] disabled:opacity-50"
            >
              Zrušit
            </button>
          </div>
        </div>
      )}
      {write.error && (
        <p role="alert" className="mt-2 text-[0.75rem] text-[var(--color-brick)]">
          {conflict
            ? 'Mezitím bylo o tomto rozhodnuto znovu. Seznam se načetl znovu — rozhodněte podle nejnovějšího.'
            : `Chyba: ${(write.error as Error).message}`}
        </p>
      )}
    </div>
  );
}

/* ------------------------------------------------------------ the consequence */

/* A ruling production does not reflect, with the existing tool that would: a
 * negative on one property is split (the engine never splits, decision 9); a
 * `same` on two properties is merged. Neither is a ruling of its own making —
 * both routes write their ruling as they always do. */
function Consequence({ row }: { row: RulingPairRow }) {
  const qc = useQueryClient();
  const [armed, setArmed] = useState<{ kind: 'detach'; listing: number } | { kind: 'merge' } | null>(
    null,
  );
  const [reason, setReason] = useState('');
  const [done, setDone] = useState<string | null>(null);
  const act = useMutation({
    mutationFn: async () => {
      if (!armed) return null;
      if (armed.kind === 'merge') {
        const res = await mergePropertySet([row.property_lo!, row.property_hi!]);
        return `Sloučeno do nemovitosti #${res.survivor_id}.`;
      }
      const res = await detachListing(row.property_lo!, armed.listing, reason.trim() || undefined);
      return res.detached
        ? `Inzerát #${armed.listing} oddělen → nemovitost #${res.restored_property_id}.`
        : detachOutcomeNote(res.outcome);
    },
    onSuccess: (text) => {
      setDone(text);
      setArmed(null);
      setReason('');
      refreshAfterDetach(qc);
    },
  });

  if (row.status !== 'standing' || row.property_lo == null || row.property_hi == null) return null;
  const negative = NEGATIVE_VERDICTS.includes(row.verdict);
  const needsSplit = negative && row.together_now;
  const needsMerge = row.verdict === 'same' && !row.together_now;
  if (!needsSplit && !needsMerge) return done ? <p className="mt-2 text-[0.75rem]">{done}</p> : null;
  const many = (row.adverts_on_property ?? 2) > 2;

  return (
    <div className="mt-3 rounded-[var(--radius-sm)] border border-[var(--color-brick)]/30 bg-[var(--color-brick-soft)]/40 px-3 py-2">
      <p className="text-[0.75rem] text-[var(--color-ink-2)]">
        {needsSplit
          ? `Rozhodnuto „Různé“, ale inzeráty jsou teď v jedné nemovitosti (#${row.property_lo}). Engine sám nerozděluje.`
          : `Rozhodnuto „Stejné“, ale inzeráty jsou v různých nemovitostech (#${row.property_lo}, #${row.property_hi}).`}
      </p>
      {!armed && (
        <div className="mt-2 flex flex-wrap gap-2">
          {needsSplit ? (
            [row.listing_lo, row.listing_hi].map((id) => (
              <button
                key={id}
                type="button"
                onClick={() => setArmed({ kind: 'detach', listing: id })}
                className="rounded-[var(--radius-sm)] border border-[var(--color-brick)] px-3 py-1 text-[0.78rem] text-[var(--color-brick)] hover:bg-[var(--color-brick-soft)]"
              >
                Rozdělit: oddělit #{id}
              </button>
            ))
          ) : (
            <button
              type="button"
              onClick={() => setArmed({ kind: 'merge' })}
              className="rounded-[var(--radius-sm)] border border-[var(--color-sage)] px-3 py-1 text-[0.78rem] text-[var(--color-sage)] hover:bg-[var(--color-sage-soft)]"
            >
              Sloučit #{row.property_lo} a #{row.property_hi}
            </button>
          )}
        </div>
      )}
      {armed && (
        <div role="group" aria-label="Potvrdit" className="mt-2">
          <p className="text-[0.78rem] text-[var(--color-ink-2)]">
            {armed.kind === 'merge'
              ? `Sloučit nemovitosti #${row.property_lo} a #${row.property_hi} do starší z nich?`
              : `Oddělit inzerát #${armed.listing} z nemovitosti #${row.property_lo}?${
                  many
                    ? ` Nemovitost má ${fmtCount(row.adverts_on_property)} ${inzeratu(row.adverts_on_property ?? 0)}: oddělený inzerát bude zapsán jako různý od všech, které zůstanou.`
                    : ''
                }`}
          </p>
          {armed.kind === 'detach' && (
            <textarea
              aria-label="Důvod rozdělení (nepovinné)"
              placeholder="Důvod (nepovinné)"
              maxLength={DETACH_REASON_MAX}
              rows={2}
              value={reason}
              disabled={act.isPending}
              onChange={(e) => setReason(e.target.value)}
              className="mt-2 block w-full max-w-[36rem] rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-2 py-1 text-[0.78rem]"
            />
          )}
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              autoFocus
              disabled={act.isPending}
              onClick={() => act.mutate()}
              className="rounded-[var(--radius-sm)] border border-[var(--color-ink-2)] px-3 py-1 text-[0.78rem] disabled:opacity-50"
            >
              {act.isPending ? 'Probíhá…' : armed.kind === 'merge' ? 'Ano, sloučit' : 'Ano, rozdělit'}
            </button>
            <button
              type="button"
              disabled={act.isPending}
              onClick={() => setArmed(null)}
              className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-3 py-1 text-[0.78rem] text-[var(--color-ink-2)]"
            >
              Zrušit
            </button>
          </div>
        </div>
      )}
      {act.error && (
        <p role="alert" className="mt-2 text-[0.75rem] text-[var(--color-brick)]">
          Chyba: {(act.error as Error).message}
        </p>
      )}
      {done && <p className="mt-2 text-[0.75rem] text-[var(--color-ink-2)]">{done}</p>}
    </div>
  );
}

/* -------------------------------------------------------------- a pair ruling */

const ENGINE_VIEW: Record<RulingPairRow['engine_view'], string> = {
  together: 'jedna skupina',
  apart: 'odděleně',
  unseen: 'inzeráty neviděl',
};

function address(street: string | null, cp: string | null): string | null {
  if (!street && !cp) return null;
  return [street, cp && `čp. ${cp}`].filter(Boolean).join(' ');
}

function PairRulingCard({
  row,
  generation,
  lo,
  hi,
}: {
  row: RulingPairRow;
  generation: string;
  lo: AutodedupMember;
  hi: AutodedupMember;
}) {
  const place = [row.obec_name, row.cast_obce_name].filter(Boolean).join(' · ');
  const streets: Record<number, string | null> = {
    [row.listing_lo]: address(row.street_lo, row.cp_lo),
    [row.listing_hi]: address(row.street_hi, row.cp_hi),
  };
  const props: Record<number, number | null> = {
    [row.listing_lo]: row.property_lo,
    [row.listing_hi]: row.property_hi,
  };
  /* A correction supersedes the pair's own newest row; an implied pair or a bare
   * veto has none, so its first pair ruling is written without one. */
  const supersedes = row.ruling_kind === 'pair' ? row.ruling_id : null;
  const options = corrections(row.verdict, row.status, 'pair');

  return (
    <li
      className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] p-4"
      data-testid={`ruling-${row.listing_lo}-${row.listing_hi}`}
    >
      <RulingHead
        verdict={row.verdict}
        status={row.status}
        source={row.source}
        agreement={row.agreement}
        decidedBy={row.decided_by}
        decidedAt={row.decided_at}
        note={row.note}
        reasons={row.reasons}
      >
        {place && <span className="text-[0.72rem] text-[var(--color-ink-3)]">{place}</span>}
      </RulingHead>

      <div className="mt-3">
        <MemberGrid
          members={[lo, hi]}
          columns="sm:grid-cols-2"
          renderUnder={(m) => (
            <p className="text-[0.7rem] text-[var(--color-ink-3)]">
              {streets[m.listing_id] ?? 'adresa neznámá'}
              {props[m.listing_id] != null && (
                <>
                  {' · '}
                  <Link
                    to={propertyPath(props[m.listing_id]!, m.listing_id)}
                    className="text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
                  >
                    nemovitost #{props[m.listing_id]}
                  </Link>
                </>
              )}
            </p>
          )}
        />
      </div>

      <dl className="mt-3 grid gap-x-6 gap-y-1 text-[0.75rem] sm:grid-cols-[auto_1fr]">
        <dt className="text-[var(--color-ink-3)]">Nyní</dt>
        <dd className="text-[var(--color-ink-2)]">
          {row.together_now
            ? `jedna nemovitost (#${row.property_lo})`
            : 'dvě různé nemovitosti'}
          {row.must_not_link ? ' · zákaz spojení platí' : ''}
        </dd>
        <dt className="text-[var(--color-ink-3)]">Engine ({row.generation ?? generation ?? '—'})</dt>
        <dd className="text-[var(--color-ink-2)]">
          {ENGINE_VIEW[row.engine_view]}
          {row.zone
            ? ` · pár: ${row.zone}${row.score != null ? ` ${row.score.toFixed(2)}` : ''}${
                row.certificate ? ` · certifikát ${row.certificate}` : ''
              }${row.decision && !row.certificate ? ` · ${row.decision}` : ''}${
                row.why_not_merged && row.zone !== 'merge' ? ` — ${row.why_not_merged}` : ''
              }`
            : ' · pár bez uloženého řádku'}
        </dd>
      </dl>
      {row.source === 'implied' && (
        <p className="mt-2 text-[0.72rem] text-[var(--color-ink-3)]">
          Plyne z potvrzené skupiny {row.group_cluster_key} ({row.group_generation ?? 'bez generace'}); engine
          ho jako vazbu nečte.{' '}
          <Link
            to={rulingsHref({ grain: 'group', listing: String(row.listing_lo) })}
            className="text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
          >
            skupina
          </Link>{' '}
          · oprava níže se zapíše k páru.
        </p>
      )}

      <div className="mt-2 flex flex-wrap gap-3 text-[0.72rem]">
        <Link
          to={pairHref(row.listing_lo, row.listing_hi, generation)}
          className="text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
        >
          důkazy páru
        </Link>
        {row.merge_group_id && (
          <Link
            to={rulingsHref({ merge_group: row.merge_group_id })}
            className="text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
          >
            celé sloučení
          </Link>
        )}
      </div>

      <CorrectionBar
        options={options}
        build={(verdict, note) => ({
          kind: 'pair',
          verdict,
          note,
          listing_lo: row.listing_lo,
          listing_hi: row.listing_hi,
          supersedes,
        })}
      />
      <Consequence row={row} />
      <History rows={row.history} />
    </li>
  );
}

/* ------------------------------------------------------------- a group ruling */

function GroupRulingCard({
  row,
  members,
  onPairs,
}: {
  row: RulingGroupRow;
  members: AutodedupMember[];
  onPairs: (mergeGroup: string) => void;
}) {
  const place = [row.obec_name, row.cast_obce_name].filter(Boolean).join(' · ');
  const options = row.set_recorded
    ? corrections(row.verdict, row.status, 'group')
    : corrections(row.verdict, row.status, 'group').filter((c) => c.verdict === 'unsure');
  return (
    <li
      className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] p-4"
      data-testid={`ruling-group-${row.ruling_key}`}
    >
      <RulingHead
        verdict={row.verdict}
        status={row.status}
        source={row.source}
        agreement={row.agreement}
        decidedBy={row.decided_by}
        decidedAt={row.decided_at}
        note={row.note}
        reasons={row.reasons}
      >
        <span className="text-[0.72rem] text-[var(--color-ink-3)] tabular-nums">
          {row.source === 'group'
            ? `skupina ${row.cluster_key} · ${row.generation ?? 'bez generace'}`
            : `sloučení ${row.merge_group_id?.slice(0, 8)}`}
          {place ? ` · ${place}` : ''}
        </span>
      </RulingHead>

      <div className="mt-3">
        {row.set_recorded ? (
          <MemberGrid members={members} />
        ) : (
          <p className="text-[0.75rem] text-[var(--color-ink-3)]">
            Sestava inzerátů nebyla zaznamenána (rozhodnutí z doby před migrací 538).
          </p>
        )}
      </div>

      <p className="mt-3 text-[0.75rem] text-[var(--color-ink-2)] tabular-nums">
        Nyní: {fmtCount(row.n_properties)}{' '}
        {row.n_properties === 1 ? 'nemovitost' : row.n_properties <= 4 ? 'nemovitosti' : 'nemovitostí'}
        {row.property_ids.length > 0 && ' ('}
        {row.property_ids.map((pid, i) => (
          <span key={pid}>
            {i > 0 && ', '}
            <Link
              to={propertyPath(pid)}
              className="text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
            >
              #{pid}
            </Link>
          </span>
        ))}
        {row.property_ids.length > 0 && ')'} · engine: {ENGINE_VIEW[row.engine_view]} (
        {fmtCount(row.n_grouped)} z {fmtCount(row.n_members)} ve skupině,{' '}
        {fmtCount(row.n_engine_groups)} {row.n_engine_groups === 1 ? 'skupina' : 'skupin'})
      </p>

      {row.source === 'browse_merge' ? (
        <div className="mt-3 text-[0.75rem] text-[var(--color-ink-3)]">
          Sloučení z Browse se opravuje po párech.{' '}
          <button
            type="button"
            onClick={() => row.merge_group_id && onPairs(row.merge_group_id)}
            className="text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
          >
            Zobrazit páry tohoto sloučení
          </button>
        </div>
      ) : (
        <CorrectionBar
          options={options}
          disabledReason={options.length === 0 ? 'Bez zaznamenané sestavy lze rozhodnutí jen odvolat.' : null}
          build={(verdict, note) => ({
            kind: 'cluster',
            verdict,
            note,
            supersedes: row.ruling_id,
          })}
        />
      )}
      <History rows={row.history} />
    </li>
  );
}
