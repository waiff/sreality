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
 * The split is the operator's, on the property page: each card links to its
 * split dialog (MS18) with the engine's groups as letters (group 1 → A, …),
 * where the preview says where each letter and each of the operator's items
 * goes, the letters can be changed, and the split is made. */

import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';

import ErrorBanner from '@/components/ErrorBanner';
import Spinner from '@/components/Spinner';
import MemberGrid from '@/components/autodedup/MemberGrid';
import { UNIT_LETTERS } from '@/components/autodedup/UnitSplit';
import {
  getProposedSplits,
  type AutodedupMember,
  type ProposedSplit,
  type ProposedSplitAdvert,
  type SplitLetters,
} from '@/lib/api';
import { fmtCount } from '@/lib/format';
import { inzeratu, splitPath } from '@/lib/mergedAdverts';
import { autodedupKeys } from '@/lib/autodedupKeys';
import Notice, { StoreNotReady } from '@/components/autodedup/Notice';
import { PropertyLinks, SplitReasons } from '@/components/autodedup/SplitCardParts';
import { useAdvertMembers } from '@/components/autodedup/useAdvertMembers';

const PAGE_SIZE = 20;

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

/* The engine's groups as letters, group n → the n-th letter (the 26th and on → Z). */
function proposalLetters(item: ProposedSplit): SplitLetters {
  const letters: SplitLetters = {};
  for (const g of groupsOf(item)) {
    for (const a of g.adverts) letters[a.listing_id] = UNIT_LETTERS[Math.min(g.n, 26) - 1];
  }
  return letters;
}

export default function AutodedupProposedSplits() {
  const [cursors, setCursors] = useState<Array<number | null>>([null]);
  const after = cursors[cursors.length - 1];
  const q = useQuery({
    queryKey: autodedupKeys.proposedSplitsPage(after),
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
  const { member, error: detailsError } = useAdvertMembers(ids);

  return (
    <div className="px-6 pt-5 pb-10 max-w-screen-xl mx-auto">
      <header>
        <h1 className="text-2xl leading-tight">AUTODEDUP · Návrhy rozdělení</h1>
        <p className="mt-1 text-sm text-[var(--color-ink-2)] leading-relaxed max-w-[52rem]">
          Nemovitosti, jejichž inzeráty by engine po poslední generaci rozdělil. Engine sám nikdy
          nerozděluje: rozhodujete vy, na stránce nemovitosti. „Rozdělit na stránce nemovitosti“
          ji otevře s písmeny podle skupin engine (skupina 1 = A, skupina 2 = B, …); tam uvidíte,
          kam které písmeno a která vaše položka odejde, písmena můžete změnit a rozdělení
          potvrdíte.
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
      {q.data && !q.data.store_ready && <StoreNotReady />}
      {page && items.length === 0 && (
        <Notice>
          {page.withheld
            ? `Živý proud engine ještě není v provozu — návrhy zatím nejsou (${page.withheld}).`
            : 'Žádné návrhy rozdělení.'}
        </Notice>
      )}

      {detailsError && <ErrorBanner message={`Údaje inzerátů: ${detailsError.message}`} />}

      <ul className="mt-4 space-y-5">
        {items.map((item) => (
          <ProposalCard key={item.property_id} item={item} member={member} />
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

function ProposalCard({
  item,
  member,
}: {
  item: ProposedSplit;
  member: (a: ProposedSplitAdvert) => AutodedupMember;
}) {
  const groups = groupsOf(item);
  const adverts = groups.reduce((n, g) => n + g.adverts.length, 0);
  const uncompared =
    item.splits.length > 0 && item.splits.every((s) => s.reason_source === 'not_compared');
  return (
    <li
      className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] p-4"
      data-testid={`proposal-${item.property_id}`}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="text-sm text-[var(--color-ink)]">Nemovitost #{item.property_id}</span>
        <PropertyLinks propertyId={item.property_id} />
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
        <Link
          to={splitPath(item.property_id, proposalLetters(item))}
          className="rounded-[var(--radius-sm)] border border-[var(--color-brick)] px-3 py-1 text-[0.8rem] text-[var(--color-brick)] transition-colors hover:bg-[var(--color-brick-soft)]"
        >
          Rozdělit na stránce nemovitosti
        </Link>
      </div>

      <div className="mt-3 space-y-3">
        {groups.map((g) => (
          <section key={g.key}>
            <p className="mb-1.5 text-[0.7rem] uppercase tracking-[0.12em] text-[var(--color-ink-3)]">
              {g.unseen ? `Skupina ${g.n} · engine neviděl` : `Skupina ${g.n}`} ·{' '}
              {UNIT_LETTERS[Math.min(g.n, 26) - 1]}
            </p>
            <MemberGrid members={g.adverts.map(member)} />
          </section>
        ))}
      </div>

      <SplitReasons splits={item.splits} />
    </li>
  );
}
