/* AUTODEDUP · Návrhy rozdělení — decision 9: auto-merge yes, auto-split never.
 *
 * After a refit or a re-run the engine may group a live property's adverts
 * apart. It never acts on that: this page lists each such property (the newest
 * pass, `GET /autodedup/proposed-splits`) with its adverts as the engine groups
 * them — photos side by side (MemberGrid), the group of the canonical advert
 * first, the adverts the engine never saw as groups of their own at the end —
 * the engine's stated reason per split pair and whether the operator has
 * already ruled on it.
 *
 * The split is the operator's statement, per card: tick the adverts to separate
 * (per advert, or a whole group at once). The ticked adverts of ONE group leave
 * together as one property, those of different groups as different properties,
 * and the unticked rest is confirmed as ONE property — nothing ticked is
 * "confirm as one", which stops the proposal. The ticks start at the proposal.
 * Selected cards run through ONE route, `POST /properties/{id}/split` (E919),
 * one call per card, each all or nothing, behind a two-step confirm with one
 * optional shared reason. The result names, per unit, the property it sits on
 * now (a link), offers the undo the server issued, and asks again when a card
 * would take back the operator's own earlier "různé" (E52). */

import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import ErrorBanner from '@/components/ErrorBanner';
import Spinner from '@/components/Spinner';
import MemberGrid from '@/components/autodedup/MemberGrid';
import { VERDICT_LABELS, displayVerdict } from '@/components/autodedup/VerdictButtons';
import {
  DETACH_REASON_MAX,
  getProposedSplits,
  splitProperty,
  splitRefusal,
  undoSplit,
  type AutodedupMember,
  type AutodedupVerdictValue,
  type ProposedSplit,
  type ProposedSplitAdvert,
  type SplitRefusal,
  type SplitResult,
  type SplitStatement,
  type SplitUndoResult,
} from '@/lib/api';
import { fmtCount } from '@/lib/format';
import { useListingPhotos } from '@/lib/hydration/useCardHydration';
import { propertyPath } from '@/lib/listingUrl';
import {
  inzeratu,
  mergedAdvertsKeys,
  refreshAfterSplit,
  unitLanding,
  unmovedReason,
} from '@/lib/mergedAdverts';
import { fetchListingsForListingIds } from '@/lib/queries';
import type { ImagePublic, ListingPublic } from '@/lib/types';

const PAGE_SIZE = 20;
/* The frames each member card pages, as the review queues ship them. */
const PHOTOS_PER_ADVERT = 12;

const REASON_SOURCE: Record<ProposedSplit['splits'][number]['reason_source'], string> = {
  conflict: 'konflikt',
  pair: 'dvojice',
  must_not_link: 'zákaz sloučení',
  none: 'bez uvedeného důvodu',
  not_compared: 'neporovnáno',
};

/* The outcomes that can never move an advert: it cannot be ticked. `last_native`
 * stays tickable — the server keeps the record with the unit holding the
 * property's own advert and sends the rest home. */
const NEVER_MOVES = new Set(['on_origin', 'moved_since', 'origin_moved_on']);
const movable = (a: ProposedSplitAdvert) => !NEVER_MOVES.has(a.detach_outcome ?? '');

type Group = { key: string; n: number; unseen: boolean; adverts: ProposedSplitAdvert[] };

/* The engine's groups, then each advert it never saw as a group of its own. */
function groupsOf(item: ProposedSplit): Group[] {
  return [
    ...item.groups.map((g, i) => ({
      key: `g-${g.cluster_key ?? `solo-${g.adverts[0]?.listing_id}`}`,
      n: i + 1,
      unseen: false,
      adverts: g.adverts,
    })),
    ...item.unseen.map((a, i) => ({
      key: `unseen-${a.listing_id}`,
      n: item.groups.length + i + 1,
      unseen: true,
      adverts: [a],
    })),
  ];
}

/* The ticks the proposal starts from. The group holding the property's own
 * adverts (no merge brought them) is the kept one, else the canonical advert's.
 * Every other group a split pair states apart from it — by a reason other than
 * `not_compared`, which states nothing — has its movable adverts ticked, a group
 * of two or more whole. An advert the engine never saw is never pre-ticked. */
function defaultTicks(item: ProposedSplit): Set<number> {
  const kept = Math.max(0, item.groups.findIndex((g) => g.adverts.some((a) => a.origin_property_id == null)));
  const stays = new Set(item.groups[kept]?.adverts.map((a) => a.listing_id));
  const apart = new Set(
    item.splits.filter((s) => s.reason_source !== 'not_compared').flatMap((s) =>
      stays.has(s.listing_lo) ? [s.listing_hi] : stays.has(s.listing_hi) ? [s.listing_lo] : [],
    ),
  );
  const ticks = new Set<number>();
  item.groups.forEach((g, i) => {
    if (i === kept || !g.adverts.some((a) => apart.has(a.listing_id))) return;
    for (const a of g.adverts) if (a.splittable) ticks.add(a.listing_id);
  });
  return ticks;
}

type Plan = {
  statement: SplitStatement;
  units: ProposedSplitAdvert[][];
  kept: ProposedSplitAdvert[];
};

/* The card's statement: the ticked adverts of each group are one unit, the rest
 * is kept together. */
function planOf(item: ProposedSplit, ticks: ReadonlySet<number>): Plan {
  const groups = groupsOf(item);
  const all = groups.flatMap((g) => g.adverts);
  const units = groups.map((g) => g.adverts.filter((a) => ticks.has(a.listing_id))).filter((u) => u.length > 0);
  return {
    statement: {
      adverts: all.map((a) => a.listing_id),
      separate: units.map((u) => u.map((a) => a.listing_id)),
      keep_together: true,
    },
    units,
    kept: all.filter((a) => !ticks.has(a.listing_id)),
  };
}

function planLine(plan: Plan): string {
  if (plan.kept.length === 0) return 'Nelze: jedna skupina musí zůstat.';
  if (plan.units.length === 0) return 'Plán: potvrdit jako jednu nemovitost';
  const tag = (a: ProposedSplitAdvert) => `#${a.listing_id} (${a.source})`;
  return (
    'Plán: ' +
    plan.units.map((u) => `oddělit ${u.map(tag).join(' + ')}`).join(' · ') +
    ` · zbytek (${plan.kept.map((a) => `#${a.listing_id}`).join(', ')}) potvrdit jako jednu nemovitost`
  );
}

type Outcome = { propertyId: number; statement: SplitStatement; sources: Record<number, string> } & (
  | { kind: 'ok'; result: SplitResult }
  | { kind: 'undone'; result: SplitUndoResult }
  | { kind: 'reverses'; refusal: SplitRefusal }
  | { kind: 'stale' }
  | { kind: 'error'; message: string }
);
type Base = Pick<Outcome, 'propertyId' | 'statement' | 'sources'>;

/* One card's statement, never throwing: a refusal is an outcome the panel shows. */
async function state(base: Base): Promise<Outcome> {
  try {
    return { ...base, kind: 'ok', result: await splitProperty(base.propertyId, base.statement) };
  } catch (e) {
    const refusal = splitRefusal(e);
    if (refusal?.code === 'reverses_rulings') return { ...base, kind: 'reverses', refusal };
    if (refusal?.code === 'stale') return { ...base, kind: 'stale' };
    return { ...base, kind: 'error', message: (e as Error).message };
  }
}

export default function AutodedupProposedSplits() {
  const qc = useQueryClient();
  const [cursors, setCursors] = useState<Array<number | null>>([null]);
  const after = cursors[cursors.length - 1];
  const q = useQuery({
    queryKey: ['autodedup', 'proposed-splits', after],
    queryFn: () => getProposedSplits({ after, limit: PAGE_SIZE }),
    staleTime: 30_000,
  });
  const page = q.data?.data ?? null;
  const items = useMemo(() => page?.items ?? [], [page]);

  /* Every advert on the page in one facts read and one photo read. */
  const ids = useMemo(
    () =>
      items.flatMap((i) => [...i.groups.flatMap((g) => g.adverts), ...i.unseen]).map((a) => a.listing_id),
    [items],
  );
  const detailsQ = useQuery<Map<number, ListingPublic>, Error>({
    queryKey: mergedAdvertsKeys.listings(ids),
    queryFn: () => fetchListingsForListingIds(ids),
    enabled: ids.length > 0,
    staleTime: 60_000,
  });
  const { photos } = useListingPhotos(ids, PHOTOS_PER_ADVERT);

  const [selected, setSelected] = useState<ReadonlySet<number>>(new Set());
  const [edited, setEdited] = useState<ReadonlyMap<number, ReadonlySet<number>>>(new Map());
  const [armed, setArmed] = useState(false);
  const [reason, setReason] = useState('');
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [outcomes, setOutcomes] = useState<Outcome[]>([]);

  const ticksOf = (item: ProposedSplit) => edited.get(item.property_id) ?? defaultTicks(item);
  const chosen = items
    .filter((i) => selected.has(i.property_id))
    .map((item) => ({ item, plan: planOf(item, ticksOf(item)) }));
  const blocked = chosen.filter((c) => c.plan.kept.length === 0);
  const leaving = chosen.reduce((n, c) => n + c.plan.units.flat().length, 0);
  const confirmOnly = chosen.filter((c) => c.plan.units.length === 0).length;

  const run = useMutation({
    mutationFn: async () => {
      const note = reason.trim() || undefined;
      const out: Outcome[] = [];
      setProgress({ done: 0, total: chosen.length });
      for (const { item, plan } of chosen) {
        const sources = Object.fromEntries(
          groupsOf(item).flatMap((g) => g.adverts).map((a) => [a.listing_id, a.source]),
        );
        out.push(
          await state({
            propertyId: item.property_id,
            statement: { ...plan.statement, ...(note ? { reason: note } : {}) },
            sources,
          }),
        );
        setProgress({ done: out.length, total: chosen.length });
      }
      return out;
    },
    onSuccess: (out) => {
      setOutcomes(out);
      setSelected(new Set());
      setEdited((prev) => {
        const next = new Map(prev);
        for (const o of out) next.delete(o.propertyId);
        return next;
      });
      setArmed(false);
      setReason('');
      refreshAfterSplit(qc);
    },
    onSettled: () => setProgress(null),
  });

  /* The panel's second word on one card: take a split back, or re-send one that
   * would take back an earlier "různé" of the operator's own. */
  const followUp = useMutation({
    mutationFn: async (o: Outcome): Promise<Outcome> => {
      const base: Base = { propertyId: o.propertyId, statement: o.statement, sources: o.sources };
      if (o.kind === 'reverses') return state({ ...base, statement: { ...o.statement, confirm_retract: true } });
      if (o.kind !== 'ok' || !o.result.undo) return o;
      try {
        return { ...base, kind: 'undone', result: await undoSplit(o.result.property_id, o.result.undo) };
      } catch (e) {
        return splitRefusal(e)?.code === 'stale'
          ? { ...base, kind: 'stale' }
          : { ...base, kind: 'error', message: (e as Error).message };
      }
    },
    onSuccess: (next) => {
      setOutcomes((prev) => prev.map((o) => (o.propertyId === next.propertyId ? next : o)));
      refreshAfterSplit(qc);
    },
  });

  const toggleCard = (pid: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(pid)) next.delete(pid);
      else next.add(pid);
      return next;
    });
  const setTicks = (item: ProposedSplit, ids: number[], on: boolean) =>
    setEdited((prev) => {
      const ticks = new Set(ticksOf(item));
      for (const id of ids) {
        if (on) ticks.add(id);
        else ticks.delete(id);
      }
      return new Map(prev).set(item.property_id, ticks);
    });

  return (
    <div className="px-6 pt-5 pb-10 max-w-screen-xl mx-auto">
      <header>
        <h1 className="text-2xl leading-tight">AUTODEDUP · Návrhy rozdělení</h1>
        <p className="mt-1 text-sm text-[var(--color-ink-2)] leading-relaxed max-w-[52rem]">
          Nemovitosti, jejichž inzeráty by engine po poslední generaci rozdělil. Engine sám nikdy
          nerozděluje: rozhodujete vy. U každého inzerátu (nebo celé skupiny) zaškrtněte „Oddělit“.
          Zaškrtnuté inzeráty jedné skupiny odejdou spolu jako jedna nemovitost (každý se vrátí
          tam, odkud přišel, nebo dostane novou), inzeráty různých skupin jako různé nemovitosti a
          zapíše se mezi nimi pravidlo „různé“. Zbytek se potvrdí jako jedna nemovitost („stejné“)
          — bez zaškrtnutí je to potvrzení celé nemovitosti a návrh zmizí.
        </p>
        {page && (
          <p className="mt-2 text-[0.75rem] text-[var(--color-ink-3)] tabular-nums">
            generace {page.generation ?? '—'} · {fmtCount(page.total)} návrhů
          </p>
        )}
      </header>

      {q.error && <ErrorBanner message={q.error.message} />}
      {q.isLoading && (
        <p className="mt-6 flex items-center gap-2 text-sm text-[var(--color-ink-3)]">
          <Spinner /> Načítám návrhy…
        </p>
      )}
      {q.data && !q.data.store_ready && (
        <p className="mt-6 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4 text-sm text-[var(--color-ink-2)]">
          Úložiště programu v této databázi zatím není — není co navrhnout.
        </p>
      )}
      {page && items.length === 0 && (
        <p className="mt-6 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4 text-sm text-[var(--color-ink-2)]">
          {page.withheld
            ? `Živý proud engine ještě není v provozu — návrhy zatím nejsou (${page.withheld}).`
            : 'Žádné návrhy rozdělení.'}
        </p>
      )}

      {outcomes.length > 0 && (
        <OutcomePanel
          outcomes={outcomes}
          busy={followUp.isPending}
          onFollowUp={(o) => followUp.mutate(o)}
        />
      )}

      {items.length > 0 && (
        <div className="sticky top-0 z-[2] mt-6 flex flex-wrap items-center gap-3 border-b border-[var(--color-rule)] bg-[var(--color-paper)] py-2">
          <span className="text-sm text-[var(--color-ink-2)] tabular-nums">
            Vybráno {fmtCount(chosen.length)} · oddělit {fmtCount(leaving)} {inzeratu(leaving)} · potvrdit{' '}
            {fmtCount(confirmOnly)} jako jednu
          </span>
          {!armed && (
            <button
              type="button"
              disabled={chosen.length === 0 || blocked.length > 0 || run.isPending}
              onClick={() => setArmed(true)}
              className="rounded-[var(--radius-sm)] border border-[var(--color-brick)] px-3 py-1 text-[0.8rem] text-[var(--color-brick)] transition-colors hover:bg-[var(--color-brick-soft)] disabled:opacity-40"
            >
              Provést vybrané
            </button>
          )}
          {blocked.length > 0 && (
            <span className="text-[0.75rem] text-[var(--color-brick)]">
              U {blocked.map((c) => `#${c.item.property_id}`).join(', ')} musí jedna skupina zůstat.
            </span>
          )}
          {progress && (
            <span className="flex items-center gap-2 text-sm text-[var(--color-ink-3)] tabular-nums">
              <Spinner /> Provádím {progress.done} / {progress.total}…
            </span>
          )}
        </div>
      )}

      {armed && (
        <div
          role="group"
          aria-label="Potvrdit provedení"
          className="mt-3 rounded-[var(--radius-sm)] border border-[var(--color-brick)]/40 bg-[var(--color-brick-soft)] px-4 py-3"
        >
          <p className="text-sm text-[var(--color-ink-2)]">
            <strong className="font-medium text-[var(--color-ink)]">
              Provést {fmtCount(chosen.length)} {chosen.length === 1 ? 'návrh' : chosen.length <= 4 ? 'návrhy' : 'návrhů'}?
            </strong>{' '}
            Oddělené inzeráty se vrátí tam, odkud přišly (ty, které nepřišly sloučením, dostanou
            novou nemovitost), a zbytek každé karty se zapíše jako jedna nemovitost.
          </p>
          <ul className="mt-1 space-y-0.5 text-[0.75rem] text-[var(--color-ink-2)]">
            {chosen.map(({ item, plan }) => (
              <li key={item.property_id}>
                #{item.property_id}: {planLine(plan)}
              </li>
            ))}
          </ul>
          <textarea
            aria-label="Společný důvod (nepovinné)"
            placeholder="Důvod (nepovinné)"
            maxLength={DETACH_REASON_MAX}
            rows={2}
            value={reason}
            disabled={run.isPending}
            onChange={(e) => setReason(e.target.value)}
            className="mt-2 block w-full max-w-[36rem] rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-2 py-1 text-[0.8rem] text-[var(--color-ink)]"
          />
          <div className="mt-2 flex items-center gap-2">
            <button
              type="button"
              autoFocus
              disabled={run.isPending}
              onClick={() => run.mutate()}
              className="rounded-[var(--radius-sm)] border border-[var(--color-brick)] px-3 py-1 text-[0.8rem] text-[var(--color-brick)] transition-colors hover:bg-[var(--color-brick)]/10 disabled:opacity-50"
            >
              {run.isPending ? 'Provádím…' : 'Ano, provést'}
            </button>
            <button
              type="button"
              disabled={run.isPending}
              onClick={() => setArmed(false)}
              className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-3 py-1 text-[0.8rem] text-[var(--color-ink-2)] transition-colors hover:bg-[var(--color-rule-soft)] disabled:opacity-50"
            >
              Zrušit
            </button>
          </div>
        </div>
      )}

      {detailsQ.isError && <ErrorBanner message={`Údaje inzerátů: ${detailsQ.error.message}`} />}

      <ul className="mt-4 space-y-5">
        {items.map((item) => (
          <ProposalCard
            key={item.property_id}
            item={item}
            ticks={ticksOf(item)}
            checked={selected.has(item.property_id)}
            disabled={armed || run.isPending}
            onToggle={() => toggleCard(item.property_id)}
            onTicks={(ids, on) => setTicks(item, ids, on)}
            member={(a) => toMember(a, detailsQ.data?.get(a.listing_id), photos.get(a.listing_id) ?? [])}
          />
        ))}
      </ul>

      {page && (cursors.length > 1 || page.next_after != null) && (
        <div className="mt-6 flex items-center gap-3 text-sm">
          <button
            type="button"
            disabled={cursors.length <= 1}
            onClick={() => setCursors((c) => c.slice(0, -1))}
            className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-3 py-1 disabled:opacity-40"
          >
            ← Předchozí
          </button>
          <button
            type="button"
            disabled={page.next_after == null}
            onClick={() => setCursors((c) => [...c, page.next_after])}
            className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-3 py-1 disabled:opacity-40"
          >
            Další →
          </button>
        </div>
      )}
    </div>
  );
}

/* A proposal's advert in the shape every review surface renders. */
function toMember(
  a: ProposedSplitAdvert,
  l: ListingPublic | undefined,
  images: ImagePublic[],
): AutodedupMember {
  return {
    listing_id: a.listing_id,
    source: a.source,
    is_active: a.is_active,
    source_url: l?.source_url ?? null,
    source_id_native: l?.source_id_native ?? null,
    sreality_id: l?.sreality_id ?? null,
    category_main: l?.category_main ?? null,
    category_type: l?.category_type ?? null,
    disposition: l?.disposition ?? null,
    area_m2: l?.area_m2 ?? null,
    floor: l?.floor ?? null,
    total_floors: l?.total_floors ?? null,
    price_czk: l?.price_czk ?? null,
    first_seen_at: l?.first_seen_at ?? null,
    last_seen_at: l?.last_seen_at ?? null,
    cover: images[0] ?? null,
    n_images: images.length,
    images: images.slice(0, PHOTOS_PER_ADVERT),
  };
}

function ProposalCard({
  item,
  ticks,
  checked,
  disabled,
  onToggle,
  onTicks,
  member,
}: {
  item: ProposedSplit;
  ticks: ReadonlySet<number>;
  checked: boolean;
  disabled: boolean;
  onToggle: () => void;
  onTicks: (ids: number[], on: boolean) => void;
  member: (a: ProposedSplitAdvert) => AutodedupMember;
}) {
  const groups = groupsOf(item);
  const plan = planOf(item, ticks);
  const adverts = groups.reduce((n, g) => n + g.adverts.length, 0);
  const uncompared =
    item.splits.length > 0 && item.splits.every((s) => s.reason_source === 'not_compared');
  return (
    <li
      className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] p-4"
      data-testid={`proposal-${item.property_id}`}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <label className="inline-flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={checked}
            disabled={disabled}
            onChange={onToggle}
            aria-label={`Vybrat nemovitost #${item.property_id}`}
          />
          <span className="text-[var(--color-ink)]">Nemovitost #{item.property_id}</span>
        </label>
        <Link
          to={propertyPath(item.property_id)}
          className="text-[0.8rem] text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2"
        >
          detail
        </Link>
        <span className="text-[0.75rem] text-[var(--color-ink-3)] tabular-nums">
          {fmtCount(adverts)} {inzeratu(adverts)} · {fmtCount(groups.length)} skupiny
        </span>
        {item.ruled && (
          <span className="rounded-[var(--radius-xs)] border border-[var(--color-rule)] px-1.5 py-0.5 text-[0.62rem] uppercase tracking-[0.08em] text-[var(--color-ink-2)]">
            rozhodnuto
          </span>
        )}
        {uncompared && (
          <span className="text-[0.72rem] text-[var(--color-ink-4)]">
            engine tyto inzeráty neporovnal — rozdělení nenavrhuje
          </span>
        )}
      </div>
      <p
        className={`mt-1 text-[0.8rem] ${
          plan.kept.length === 0 ? 'text-[var(--color-brick)]' : 'text-[var(--color-ink-2)]'
        }`}
      >
        {planLine(plan)}
      </p>

      <div className="mt-3 space-y-3">
        {groups.map((g) => {
          const canMove = g.adverts.filter(movable).map((a) => a.listing_id);
          const on = g.adverts.filter((a) => ticks.has(a.listing_id)).length;
          const all = canMove.length > 0 && canMove.every((id) => ticks.has(id));
          return (
            <section key={g.key}>
              <div className="mb-1.5 flex flex-wrap items-center gap-x-3">
                <p className="text-[0.7rem] uppercase tracking-[0.12em] text-[var(--color-ink-3)]">
                  {g.unseen ? `Skupina ${g.n} · engine neviděl` : `Skupina ${g.n}`} ·{' '}
                  {on === 0 ? 'zůstává' : on === g.adverts.length ? 'oddělit' : 'oddělit část'}
                </p>
                <label className="inline-flex items-center gap-1.5 text-[0.72rem] text-[var(--color-ink-2)]">
                  <input
                    type="checkbox"
                    checked={all}
                    disabled={disabled || canMove.length === 0}
                    ref={(el) => {
                      if (el) el.indeterminate = on > 0 && !all;
                    }}
                    onChange={() => onTicks(canMove, !all)}
                    aria-label={`Oddělit skupinu ${g.n}`}
                  />
                  Oddělit skupinu
                </label>
              </div>
              <MemberGrid
                members={g.adverts.map(member)}
                renderUnder={(m) => {
                  const a = g.adverts.find((x) => x.listing_id === m.listing_id);
                  if (!a) return null;
                  return movable(a) ? (
                    <label className="inline-flex items-center gap-1.5 text-[0.72rem] text-[var(--color-ink-2)]">
                      <input
                        type="checkbox"
                        checked={ticks.has(a.listing_id)}
                        disabled={disabled}
                        onChange={() => onTicks([a.listing_id], !ticks.has(a.listing_id))}
                        aria-label={`Oddělit inzerát #${a.listing_id}`}
                      />
                      Oddělit
                    </label>
                  ) : (
                    <p className="text-[0.66rem] text-[var(--color-ink-4)]">
                      {unmovedReason(a.detach_outcome ?? '')} — zůstane
                    </p>
                  );
                }}
              />
            </section>
          );
        })}
      </div>

      <ul className="mt-3 space-y-0.5 text-[0.75rem] text-[var(--color-ink-2)]">
        {item.splits.map((s) => (
          <li key={`${s.listing_lo}-${s.listing_hi}`}>
            <span className="font-mono tabular-nums text-[var(--color-ink-3)]">
              #{s.listing_lo} × #{s.listing_hi}
            </span>{' '}
            · {REASON_SOURCE[s.reason_source]}: {s.reason}
            {s.ruling && (
              <span className="text-[var(--color-ink-3)]">
                {' '}
                · rozhodnutí: {VERDICT_LABELS[displayVerdict(s.ruling.verdict as AutodedupVerdictValue)]} (
                {s.ruling.decided_by})
              </span>
            )}
          </li>
        ))}
      </ul>
    </li>
  );
}

const linkClass = 'text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2';

/* The last batch, card by card — it stays on screen after the list refreshes,
 * since a card the operator decided leaves the list. */
function OutcomePanel({
  outcomes,
  busy,
  onFollowUp,
}: {
  outcomes: Outcome[];
  busy: boolean;
  onFollowUp: (o: Outcome) => void;
}) {
  const done = outcomes.filter((o) => o.kind === 'ok').length;
  return (
    <section
      aria-label="Výsledek rozdělení"
      className="mt-6 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-4 py-3"
    >
      <p className="text-sm text-[var(--color-ink)]">
        Provedeno {fmtCount(done)} z {fmtCount(outcomes.length)}.
      </p>
      <ul className="mt-2 space-y-2 text-[0.75rem]">
        {outcomes.map((o) => (
          <li key={o.propertyId} data-testid={`outcome-${o.propertyId}`}>
            <p className="text-[var(--color-ink)]">Nemovitost #{o.propertyId}</p>
            <OutcomeBody outcome={o} busy={busy} onFollowUp={() => onFollowUp(o)} />
          </li>
        ))}
      </ul>
    </section>
  );
}

function OutcomeBody({
  outcome: o,
  busy,
  onFollowUp,
}: {
  outcome: Outcome;
  busy: boolean;
  onFollowUp: () => void;
}) {
  const tag = (id: number) => `#${id}${o.sources[id] ? ` (${o.sources[id]})` : ''}`;
  const button =
    'ml-2 rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-2 py-0.5 text-[0.72rem] text-[var(--color-ink-2)] hover:bg-[var(--color-rule-soft)] disabled:opacity-50';
  if (o.kind === 'ok') {
    const r = o.result;
    return (
      <div className="text-[var(--color-ink-2)]">
        <ul className="space-y-0.5">
          {r.units.map((u) => (
            <li key={u.unit}>
              {u.role === 'kept' ? 'zůstávají spolu' : 'odděleno'}: {u.listing_ids.map(tag).join(', ')} →{' '}
              <Link to={propertyPath(u.property_id)} className={linkClass}>
                {unitLanding(u, o.propertyId)}
              </Link>
            </li>
          ))}
        </ul>
        {r.undo ? (
          <button type="button" disabled={busy} onClick={onFollowUp} className={button}>
            Vrátit
          </button>
        ) : (
          <p className="text-[var(--color-ink-3)]">Beze změny — už platí.</p>
        )}
      </div>
    );
  }
  if (o.kind === 'undone') {
    const pid = o.result.property_id;
    return (
      <p className="text-[var(--color-ink-2)]">
        Vráceno — inzeráty jsou znovu jedna nemovitost
        {pid != null && (
          <>
            {' '}
            <Link to={propertyPath(pid)} className={linkClass}>
              #{pid}
            </Link>
          </>
        )}
        , rozhodnutí jsou jako předtím.
      </p>
    );
  }
  if (o.kind === 'reverses') {
    const pairs = (o.refusal.ids as number[][]).map(([lo, hi]) => `#${lo} × #${hi}`).join(', ');
    return (
      <p className="text-[var(--color-brick)]">
        Nic se nezapsalo: tím byste vzali zpět své dřívější rozhodnutí „různé“ u {pairs}.
        <button type="button" disabled={busy} onClick={onFollowUp} className={button}>
          Přesto uložit
        </button>
      </p>
    );
  }
  if (o.kind === 'stale') {
    return <p className="text-[var(--color-brick)]">Karta se mezitím změnila — načteno znovu, nic se nezapsalo.</p>;
  }
  return <p className="text-[var(--color-brick)]">Chyba: {o.message}</p>;
}
