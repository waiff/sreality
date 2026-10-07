/* AUTODEDUP · Rozdělení podle kategorií (E937).
 *
 * A property whose ads carry categories rule 15 never joins — a sale and a
 * rental of one flat, a flat and a house — is a question only the
 * operator answers, from the photos and the ads' own words. The page opens
 * from a link that names the properties (`?properties=12664,9737`; no stored
 * list) and reads them ten at a time (`GET /autodedup/category-splits`). Each
 * card shows the property's SIDES, the ads the server's one definition
 * (`category_clash`) lets be one property, with photos and facts (MemberGrid)
 * and each ad's text (MemberText); a contentless record and an ad of unknown
 * category ride with the group that stays and never make a property mixed.
 *
 * A side can bundle two flats, so every other ad carries a LETTER, as on the
 * property page: one letter is one property. The letters start one per side.
 * "Rozdělit podle písmen" opens the property page's split dialog (MS18) on these
 * letters (`?letters=`, every ad named, the riders with the letter that stays),
 * where the preview says where each letter and each of the operator's items
 * goes and the split is made. A property no longer mixed, or confirmed, is a
 * one-line done row: the list is also the progress view. */

import { useMemo, useState, type ReactNode } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';

import ErrorBanner from '@/components/ErrorBanner';
import Spinner from '@/components/Spinner';
import MemberGrid from '@/components/autodedup/MemberGrid';
import MemberText from '@/components/autodedup/MemberText';
import Notice, { StoreNotReady } from '@/components/autodedup/Notice';
import { PropertyLinks, SplitReasons } from '@/components/autodedup/SplitCardParts';
import { UnitSelect, type UnitMap } from '@/components/autodedup/UnitSplit';
import {
  PROPERTIES_PER_PAGE,
  cardState,
  clashLabel,
  defaultLetters,
  letterPlan,
  lettersLine,
  parsePropertyIds,
  rides,
  sideLabel,
  splitWithheld,
} from '@/components/autodedup/categorySplit';
import { useAdvertMembers } from '@/components/autodedup/useAdvertMembers';
import {
  getCategorySplits,
  type AutodedupMember,
  type CategorySplit,
  type CategorySplitAdvert,
  type CategorySplitSide,
} from '@/lib/api';
import { autodedupKeys } from '@/lib/autodedupKeys';
import { fmtCount } from '@/lib/format';
import { inzeratu, splitPath } from '@/lib/mergedAdverts';
import { portalLabel } from '@/lib/portals';

type MemberOf = (a: CategorySplitAdvert) => AutodedupMember;
/* The card's letters, as each ad's select needs them. */
type Letters = {
  units: UnitMap;
  count: number;
  onLetter: (id: number, letter: string) => void;
};

const brick =
  'rounded-[var(--radius-sm)] border border-[var(--color-brick)] px-3 py-1 text-[0.8rem] text-[var(--color-brick)] transition-colors hover:bg-[var(--color-brick-soft)]';
const muted = 'text-[0.72rem] text-[var(--color-ink-4)]';

/* 1 strana · 2–4 strany · 5+ stran. */
const stran = (n: number) => (n === 1 ? 'strana' : n >= 2 && n <= 4 ? 'strany' : 'stran');

export default function AutodedupCategorySplits() {
  const [params] = useSearchParams();
  const raw = params.get('properties');
  const ids = useMemo(() => parsePropertyIds(raw), [raw]);
  const pages = Math.max(1, Math.ceil(ids.length / PROPERTIES_PER_PAGE));
  /* The page index belongs to the link it pages: a new link starts at page 1. */
  const [paging, setPaging] = useState<{ raw: string | null; index: number }>({ raw, index: 0 });
  const index = paging.raw === raw ? Math.min(paging.index, pages - 1) : 0;
  const pageIds = useMemo(
    () => ids.slice(index * PROPERTIES_PER_PAGE, (index + 1) * PROPERTIES_PER_PAGE),
    [ids, index],
  );

  const q = useQuery({
    queryKey: autodedupKeys.categorySplitsPage(pageIds),
    queryFn: () => getCategorySplits(pageIds),
    enabled: pageIds.length > 0,
    staleTime: 30_000,
  });
  const page = q.data?.data ?? null;
  const items = useMemo(() => page?.items ?? [], [page]);
  const byId = useMemo(() => new Map(items.map((i) => [i.property_id, i])), [items]);

  /* The ads shown as cards, in one facts read and one photo read per page: an
   * open card's, never a done row's or a contentless record's (a line). */
  const cardIds = useMemo(
    () =>
      items
        .filter((i) => cardState(i) === 'open')
        .flatMap((i) => i.groups.flatMap((g) => g.adverts))
        .filter((a) => !a.empty)
        .map((a) => a.listing_id),
    [items],
  );
  const { member, error: detailsError } = useAdvertMembers(cardIds);

  const counts = { open: 0, split: 0, kept: 0 };
  for (const item of items) counts[cardState(item)] += 1;
  const missing = page?.missing ?? [];

  return (
    <div className="px-6 pt-5 pb-10 max-w-screen-xl mx-auto">
      <header>
        <h1 className="text-2xl leading-tight">AUTODEDUP · Rozdělení podle kategorií</h1>
        <p className="mt-1 text-sm text-[var(--color-ink-2)] leading-relaxed max-w-[52rem]">
          Nemovitosti, ve kterých jsou inzeráty různých kategorií — třeba prodej a pronájem, nebo
          byt a dům. Takové inzeráty systém do jedné nemovitosti sám nikdy nespojí.
          Prohlédněte si fotky a texty. Stejné písmeno znamená jednu nemovitost; písmena jsou
          předvyplněná podle kategorií a inzerátu, který patří jinam, třeba jinému bytu, dejte jiné
          písmeno. <strong>Rozdělit podle písmen</strong> otevře rozdělení na stránce nemovitosti:
          tam uvidíte, kam které písmeno a která vaše položka odejde, a teprve tam rozdělení
          potvrdíte.
        </p>
        {ids.length > 0 && (
          <p className="mt-2 text-[0.75rem] text-[var(--color-ink-3)] tabular-nums">
            Nemovitostí v odkazu: {fmtCount(ids.length)} · stránka {index + 1} / {pages} (po{' '}
            {PROPERTIES_PER_PAGE})
            {page && (
              <>
                {' '}
                · na této stránce: k rozhodnutí {fmtCount(counts.open)} · rozděleno{' '}
                {fmtCount(counts.split)} · ponecháno {fmtCount(counts.kept)}
              </>
            )}
          </p>
        )}
      </header>

      {ids.length === 0 && (
        <Notice>
          Tato stránka se otevírá z odkazu, který nemovitosti vyjmenuje
          (…/autodedup/category-splits?properties=12664,9737). V tomto odkazu žádné nejsou.
        </Notice>
      )}
      {q.error && <ErrorBanner message={q.error.message} />}
      {q.isLoading && (
        <p className="mt-6 flex items-center gap-2 text-sm text-[var(--color-ink-3)]">
          <Spinner /> Načítám nemovitosti…
        </p>
      )}
      {q.data && !q.data.store_ready && <StoreNotReady />}
      {detailsError && <ErrorBanner message={`Údaje inzerátů: ${detailsError.message}`} />}

      <ul className="mt-4 space-y-5">
        {pageIds
          .filter((pid) => byId.has(pid))
          .map((pid) => (
            <CategoryCard key={pid} propertyId={pid} item={byId.get(pid)} member={member} />
          ))}
      </ul>

      {missing.length > 0 && (
        <p className={`mt-4 ${muted}`}>
          Bez karty (nejsou aktivní nemovitostí se dvěma a více inzeráty):{' '}
          {missing.map((pid) => `#${pid}`).join(', ')}
        </p>
      )}

      {pages > 1 && (
        <div className="mt-6 flex items-center gap-3 text-sm">
          <button
            type="button"
            disabled={index === 0}
            onClick={() => setPaging({ raw, index: index - 1 })}
            className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-3 py-1 disabled:opacity-40"
          >
            ← Předchozí
          </button>
          <button
            type="button"
            disabled={index >= pages - 1}
            onClick={() => setPaging({ raw, index: index + 1 })}
            className="rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-3 py-1 disabled:opacity-40"
          >
            Další →
          </button>
        </div>
      )}
    </div>
  );
}

function CardHeader({ propertyId, children }: { propertyId: number; children?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
      <h2 className="text-sm text-[var(--color-ink)]">Nemovitost #{propertyId}</h2>
      <PropertyLinks propertyId={propertyId} />
      {children}
    </div>
  );
}

function CategoryCard({
  propertyId,
  item,
  member,
}: {
  propertyId: number;
  item: CategorySplit | undefined;
  member: MemberOf;
}) {
  /* The letters hold for the ads they were set over: a re-read that changed the
   * ads starts again from the sides. */
  const [letters, setLetters] = useState<{ list: string; units: UnitMap } | null>(null);
  if (!item) return null;

  const state = cardState(item);
  if (state !== 'open') {
    const side = item.groups.find((g) => g.category_type);
    return (
      <li
        data-testid={`category-${propertyId}`}
        className="rounded-[var(--radius-md)] border border-[var(--color-rule)] px-4 py-2"
      >
        <CardHeader propertyId={propertyId}>
          <span className="text-[0.75rem] text-[var(--color-ink-3)]">
            {state === 'kept'
              ? 'ponecháno jako jedna nemovitost'
              : `jen jedna kategorie (${side ? sideLabel(side) : 'neznámá kategorie'}), není co dělit`}
          </span>
        </CardHeader>
      </li>
    );
  }

  const ads = item.groups.flatMap((g) => g.adverts);
  const listKey = ads.map((a) => a.listing_id).sort((a, b) => a - b).join(',');
  const units = letters?.list === listKey ? letters.units : defaultLetters(item);
  const plan = letterPlan(item, units);
  const withheld = splitWithheld(plan);
  const lettering: Letters = {
    units,
    count: ads.filter((a) => !rides(a)).length,
    onLetter: (id, letter) => setLetters({ list: listKey, units: { ...units, [id]: letter } }),
  };
  const adverts = ads.length;
  const sides = item.groups.filter((g) => g.category_type).length;
  return (
    <li
      data-testid={`category-${propertyId}`}
      className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] p-4"
    >
      <CardHeader propertyId={propertyId}>
        <span className="text-[0.75rem] text-[var(--color-ink-3)] tabular-nums">
          {fmtCount(adverts)} {inzeratu(adverts)} · {fmtCount(sides)} {stran(sides)}
        </span>
        {item.ruled && (
          <span className="rounded-[var(--radius-xs)] border border-[var(--color-rule)] px-1.5 py-0.5 text-[0.62rem] uppercase tracking-[0.08em] text-[var(--color-ink-2)]">
            rozhodnuto
          </span>
        )}
      </CardHeader>

      <div className="mt-3 space-y-4">
        {item.groups.map((g) => (
          <SideSection
            key={g.adverts[0]?.listing_id ?? g.label}
            side={g}
            member={member}
            letters={lettering}
          />
        ))}
      </div>

      <SplitReasons
        splits={item.splits.map((s) => ({
          ...s,
          reason: clashLabel(item, s.listing_lo, s.listing_hi) ?? s.reason,
        }))}
      />
      <p className="mt-3 text-[0.75rem] text-[var(--color-ink-2)] tabular-nums">{lettersLine(plan)}</p>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        {withheld ? (
          <span className="text-[0.75rem] text-[var(--color-ink-3)]">{withheld}</span>
        ) : (
          <Link to={splitPath(propertyId, plan.letters)} className={brick}>
            Rozdělit podle písmen
          </Link>
        )}
      </div>
    </li>
  );
}

/* One side: its ads as cards, each with its letter and its own words under it,
 * its contentless records as one muted line each. */
function SideSection({
  side,
  member,
  letters,
}: {
  side: CategorySplitSide;
  member: MemberOf;
  letters: Letters;
}) {
  const cards = side.adverts.filter((a) => !a.empty);
  const empties = side.adverts.filter((a) => a.empty);
  return (
    <section aria-label={sideLabel(side)}>
      <h3 className="mb-1.5 text-[0.7rem] uppercase tracking-[0.12em] text-[var(--color-ink-3)]">
        {sideLabel(side)} · {fmtCount(cards.length)} {inzeratu(cards.length)}
      </h3>
      {cards.length > 0 && (
        /* Unfolded: the decision covers every ad, so none hides behind a fold. */
        <MemberGrid
          unfolded
          members={cards.map(member)}
          renderUnder={(m) => {
            const a = cards.find((x) => x.listing_id === m.listing_id);
            return a ? <AdUnder ad={a} letters={letters} /> : null;
          }}
        />
      )}
      {empties.map((a) => (
        <p key={a.listing_id} className={`mt-1.5 ${muted}`}>
          #{a.listing_id} · {portalLabel(a.source) ?? a.source} · prázdný záznam, bez ceny, plochy a
          textu, zůstane
        </p>
      ))}
    </section>
  );
}

function AdUnder({ ad, letters }: { ad: CategorySplitAdvert; letters: Letters }) {
  const portal = portalLabel(ad.source) ?? ad.source;
  return (
    <>
      {rides(ad) ? (
        <p className={muted}>kategorie neuvedena — bez písmena, zůstane se skupinou, která zůstává</p>
      ) : (
        <UnitSelect
          listingId={ad.listing_id}
          units={letters.units}
          count={letters.count}
          onChange={(letter) => letters.onLetter(ad.listing_id, letter)}
          label={
            <>
              Nemovitost<span className="sr-only"> inzerátu {portal} #{ad.listing_id}</span>
            </>
          }
        />
      )}
      <MemberText title={ad.text.title} text={ad.text.description} label={`#${ad.listing_id}`} />
    </>
  );
}
