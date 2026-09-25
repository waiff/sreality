/* AUTODEDUP · Návrhy rozdělení — decision 9: auto-merge yes, auto-split never.
 *
 * After a refit or a re-run the engine may group a live property's adverts
 * apart. It never acts on that: this page lists each such property (the newest
 * pass, `GET /autodedup/proposed-splits`) with its adverts as the engine groups
 * them — photos side by side (MemberGrid), the group of the canonical advert
 * first — the engine's stated reason per split pair and whether the operator
 * has already ruled on it.
 *
 * The split is the operator's: tick proposals, then "Rozdělit vybrané" detaches
 * adverts back to the property they came from (`POST /properties/{id}/detach`,
 * one advert at a time, the optional shared reason kept on each "different"
 * ruling), behind a two-step confirm, with progress and a per-advert outcome.
 * See `splitPlan` for which group stays and which adverts may leave. */

import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import ErrorBanner from '@/components/ErrorBanner';
import Spinner from '@/components/Spinner';
import MemberGrid from '@/components/autodedup/MemberGrid';
import { VERDICT_LABELS, displayVerdict } from '@/components/autodedup/VerdictButtons';
import {
  DETACH_REASON_MAX,
  detachListing,
  getProposedSplits,
  type AutodedupMember,
  type AutodedupVerdictValue,
  type ProposedSplit,
  type ProposedSplitAdvert,
} from '@/lib/api';
import { fmtCount } from '@/lib/format';
import { useListingPhotos } from '@/lib/hydration/useCardHydration';
import { propertyPath } from '@/lib/listingUrl';
import { detachOutcomeNote, inzeratu, mergedAdvertsKeys, refreshAfterDetach } from '@/lib/mergedAdverts';
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
};

/* Which group stays and which adverts a split takes away. The group holding the
 * property's own adverts (no merge brought them) stays, else the canonical
 * advert's. An advert leaves only when it is alone in its group (a detach rules
 * it different from every advert left behind, a group-mate included), a merge
 * brought it (it has somewhere to return to) and a split pair states it apart
 * from the staying group. */
function splitPlan(item: ProposedSplit): { kept: number; take: ProposedSplitAdvert[] } {
  const kept = Math.max(0, item.groups.findIndex((g) => g.adverts.some((a) => a.origin_property_id == null)));
  const stays = new Set(item.groups[kept]?.adverts.map((a) => a.listing_id));
  const apart = new Set(
    item.splits.flatMap((s) =>
      stays.has(s.listing_lo) ? [s.listing_hi] : stays.has(s.listing_hi) ? [s.listing_lo] : [],
    ),
  );
  const take = item.groups
    .filter((g, i) => i !== kept && g.adverts.length === 1)
    .map((g) => g.adverts[0])
    .filter((a) => a.origin_property_id != null && apart.has(a.listing_id));
  return { kept, take };
}

function stayNote(a: ProposedSplitAdvert, groupSize: number): string {
  if (a.origin_property_id == null) return 'nepřišel sloučením — zůstane';
  if (groupSize > 1) return 'skupinu nelze oddělit po jednom — zůstane';
  return 'engine ho od zůstávající skupiny neodlišil — zůstane';
}

type Outcome = { property_id: number; listing_id: number; ok: boolean; text: string };

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
  const [armed, setArmed] = useState(false);
  const [reason, setReason] = useState('');
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [outcomes, setOutcomes] = useState<Outcome[]>([]);

  const chosen = items.filter((i) => selected.has(i.property_id));
  const queue = chosen.flatMap((i) => splitPlan(i).take.map((a) => ({ item: i, advert: a })));

  const split = useMutation({
    mutationFn: async () => {
      const note = reason.trim() || undefined;
      const out: Outcome[] = [];
      setProgress({ done: 0, total: queue.length });
      for (const { item, advert } of queue) {
        try {
          const res = await detachListing(item.property_id, advert.listing_id, note);
          out.push({
            property_id: item.property_id,
            listing_id: advert.listing_id,
            ok: res.detached,
            text: res.detached
              ? `odděleno → nemovitost #${res.restored_property_id}`
              : detachOutcomeNote(res.outcome),
          });
        } catch (e) {
          out.push({
            property_id: item.property_id,
            listing_id: advert.listing_id,
            ok: false,
            text: `chyba: ${(e as Error).message}`,
          });
        }
        setProgress({ done: out.length, total: queue.length });
      }
      return out;
    },
    onSuccess: (out) => {
      setOutcomes(out);
      setSelected(new Set());
      setArmed(false);
      setReason('');
      refreshAfterDetach(qc);
    },
    onSettled: () => setProgress(null),
  });

  const toggle = (pid: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(pid)) next.delete(pid);
      else next.add(pid);
      return next;
    });

  return (
    <div className="px-6 pt-5 pb-10 max-w-screen-xl mx-auto">
      <header>
        <h1 className="text-2xl leading-tight">AUTODEDUP · Návrhy rozdělení</h1>
        <p className="mt-1 text-sm text-[var(--color-ink-2)] leading-relaxed max-w-[52rem]">
          Nemovitosti, jejichž inzeráty by engine po poslední generaci rozdělil. Engine sám nikdy
          nerozděluje: rozhodujete vy. Zůstává skupina s vlastními inzeráty nemovitosti (jinak ta
          s inzerátem v záhlaví). Oddělí se inzerát, který je ve skupině sám, přivedlo ho sloučení
          a engine ho od zůstávající skupiny odlišil: vrátí se do nemovitosti, ze které přišel, a
          zapíše se pravidlo „různé“.
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
          Žádné návrhy rozdělení.
        </p>
      )}

      {outcomes.length > 0 && <OutcomeList outcomes={outcomes} />}

      {items.length > 0 && (
        <div className="sticky top-0 z-[2] mt-6 flex flex-wrap items-center gap-3 border-b border-[var(--color-rule)] bg-[var(--color-paper)] py-2">
          <span className="text-sm text-[var(--color-ink-2)] tabular-nums">
            Vybráno {fmtCount(chosen.length)} · {fmtCount(queue.length)} {inzeratu(queue.length)} k
            oddělení
          </span>
          {!armed && (
            <button
              type="button"
              disabled={queue.length === 0}
              onClick={() => setArmed(true)}
              className="rounded-[var(--radius-sm)] border border-[var(--color-brick)] px-3 py-1 text-[0.8rem] text-[var(--color-brick)] transition-colors hover:bg-[var(--color-brick-soft)] disabled:opacity-40"
            >
              Rozdělit vybrané
            </button>
          )}
          {progress && (
            <span className="flex items-center gap-2 text-sm text-[var(--color-ink-3)] tabular-nums">
              <Spinner /> Rozděluji {progress.done} / {progress.total}…
            </span>
          )}
        </div>
      )}

      {armed && (
        <div
          role="group"
          aria-label="Potvrdit rozdělení"
          className="mt-3 rounded-[var(--radius-sm)] border border-[var(--color-brick)]/40 bg-[var(--color-brick-soft)] px-4 py-3"
        >
          <p className="text-sm text-[var(--color-ink-2)]">
            <strong className="font-medium text-[var(--color-ink)]">
              Oddělit {fmtCount(queue.length)} {inzeratu(queue.length)} z {fmtCount(chosen.length)}{' '}
              {chosen.length === 1 ? 'nemovitosti' : 'nemovitostí'}?
            </strong>{' '}
            Každý se vrátí do nemovitosti, ze které přišel, a zapíše se, že se zbylými inzeráty
            nejde o stejnou nemovitost.
          </p>
          <textarea
            aria-label="Společný důvod rozdělení (nepovinné)"
            placeholder="Důvod (nepovinné)"
            maxLength={DETACH_REASON_MAX}
            rows={2}
            value={reason}
            disabled={split.isPending}
            onChange={(e) => setReason(e.target.value)}
            className="mt-2 block w-full max-w-[36rem] rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper)] px-2 py-1 text-[0.8rem] text-[var(--color-ink)]"
          />
          <div className="mt-2 flex items-center gap-2">
            <button
              type="button"
              autoFocus
              disabled={split.isPending}
              onClick={() => split.mutate()}
              className="rounded-[var(--radius-sm)] border border-[var(--color-brick)] px-3 py-1 text-[0.8rem] text-[var(--color-brick)] transition-colors hover:bg-[var(--color-brick)]/10 disabled:opacity-50"
            >
              {split.isPending ? 'Rozděluji…' : 'Ano, rozdělit'}
            </button>
            <button
              type="button"
              disabled={split.isPending}
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
            checked={selected.has(item.property_id)}
            onToggle={() => toggle(item.property_id)}
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
  checked,
  onToggle,
  member,
}: {
  item: ProposedSplit;
  checked: boolean;
  onToggle: () => void;
  member: (a: ProposedSplitAdvert) => AutodedupMember;
}) {
  const { kept, take } = splitPlan(item);
  const taken = new Set(take.map((a) => a.listing_id));
  const adverts = item.groups.reduce((n, g) => n + g.adverts.length, item.unseen.length);
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
            disabled={take.length === 0}
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
          {fmtCount(adverts)} {inzeratu(adverts)} · {fmtCount(item.groups.length)} skupiny
        </span>
        {item.ruled && (
          <span className="rounded-[var(--radius-xs)] border border-[var(--color-rule)] px-1.5 py-0.5 text-[0.62rem] uppercase tracking-[0.08em] text-[var(--color-ink-2)]">
            rozhodnuto
          </span>
        )}
        {take.length === 0 && (
          <span className="text-[0.72rem] text-[var(--color-ink-4)]">
            nelze rozdělit: žádný inzerát nelze samostatně vrátit, odkud přišel
          </span>
        )}
      </div>

      <div className="mt-3 space-y-3">
        {item.groups.map((g, i) => (
          <section key={g.cluster_key ?? `solo-${g.adverts[0]?.listing_id}`}>
            <p className="mb-1.5 text-[0.7rem] uppercase tracking-[0.12em] text-[var(--color-ink-3)]">
              {i === kept
                ? 'Zůstává'
                : g.adverts.some((a) => taken.has(a.listing_id))
                  ? `Oddělit · skupina ${i + 1}`
                  : `Skupina ${i + 1}`}
            </p>
            <MemberGrid
              members={g.adverts.map(member)}
              renderUnder={(m) => {
                const a = g.adverts.find((x) => x.listing_id === m.listing_id);
                return i !== kept && a && !taken.has(a.listing_id) ? (
                  <p className="text-[0.66rem] text-[var(--color-ink-4)]">{stayNote(a, g.adverts.length)}</p>
                ) : null;
              }}
            />
          </section>
        ))}
        {item.unseen.length > 0 && (
          <p className="text-[0.72rem] text-[var(--color-ink-3)]">
            Engine neviděl (zůstávají):{' '}
            {item.unseen.map((a) => `#${a.listing_id} (${a.source})`).join(', ')}
          </p>
        )}
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

/* The last batch, advert by advert — it stays on screen after the list
 * refreshes, since a split property leaves the list. */
function OutcomeList({ outcomes }: { outcomes: Outcome[] }) {
  const ok = outcomes.filter((o) => o.ok).length;
  return (
    <section
      aria-label="Výsledek rozdělení"
      className="mt-6 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-4 py-3"
    >
      <p className="text-sm text-[var(--color-ink)]">
        Odděleno {fmtCount(ok)} z {fmtCount(outcomes.length)} {inzeratu(outcomes.length)}.
      </p>
      <ul className="mt-1 space-y-0.5 text-[0.75rem]">
        {outcomes.map((o) => (
          <li
            key={o.listing_id}
            className={o.ok ? 'text-[var(--color-ink-2)]' : 'text-[var(--color-brick)]'}
          >
            <Link to={propertyPath(o.property_id)} className="underline decoration-dotted underline-offset-2">
              #{o.property_id}
            </Link>{' '}
            · inzerát #{o.listing_id}: {o.text}
          </li>
        ))}
      </ul>
    </section>
  );
}
