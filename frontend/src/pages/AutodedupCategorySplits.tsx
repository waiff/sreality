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
 * Two decisions, each behind a second click, both the operator's split
 * statement (`POST /properties/{id}/split`, E919): split by the letters (the
 * property page's `splitPlan`: every letter but the one that stays leaves as
 * one property, ruled "různé" from the rest) or keep as one property (every
 * pair ruled "stejné"). The outcome stays in place with the server's undo. A
 * property no longer mixed, or confirmed, is a one-line done row: the list is
 * also the progress view. */

import { useMemo, useState, type ReactNode } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import ErrorBanner from '@/components/ErrorBanner';
import Spinner from '@/components/Spinner';
import MemberGrid from '@/components/autodedup/MemberGrid';
import MemberText from '@/components/autodedup/MemberText';
import Notice, { StoreNotReady } from '@/components/autodedup/Notice';
import { PropertyLinks, SplitReasons } from '@/components/autodedup/SplitCardParts';
import SplitOutcomeBody from '@/components/autodedup/SplitOutcomeBody';
import SplitPlanLines, { priceLabel } from '@/components/autodedup/SplitPlanLines';
import { UnitSelect, type UnitMap } from '@/components/autodedup/UnitSplit';
import {
  PROPERTIES_PER_PAGE,
  cardState,
  clashLabel,
  defaultLetters,
  keepSentence,
  keepStatement,
  letterPlan,
  lettersLine,
  parsePropertyIds,
  rides,
  sideLabel,
  splitWithheld,
} from '@/components/autodedup/categorySplit';
import { followUpSplit, sendSplit, type SplitOutcome } from '@/components/autodedup/splitOutcome';
import { useAdvertMembers } from '@/components/autodedup/useAdvertMembers';
import {
  getCategorySplits,
  type AutodedupMember,
  type CategorySplit,
  type CategorySplitAdvert,
  type CategorySplitSide,
  type SplitStatement,
} from '@/lib/api';
import { autodedupKeys } from '@/lib/autodedupKeys';
import { fmtCount } from '@/lib/format';
import { inzeratu, refreshAfterSplit, saysUnmoved, unmovedReason } from '@/lib/mergedAdverts';
import { portalLabel } from '@/lib/portals';

type Decision = 'split' | 'keep';
type MemberOf = (a: CategorySplitAdvert) => AutodedupMember;
/* The card's letters, as each ad's select needs them. */
type Letters = {
  units: UnitMap;
  count: number;
  disabled: boolean;
  onLetter: (id: number, letter: string) => void;
};

const button =
  'rounded-[var(--radius-sm)] border px-3 py-1 text-[0.8rem] transition-colors disabled:opacity-40';
const brick = `${button} border-[var(--color-brick)] text-[var(--color-brick)] hover:bg-[var(--color-brick-soft)]`;
const plain = `${button} border-[var(--color-rule)] text-[var(--color-ink-2)] hover:bg-[var(--color-rule-soft)]`;
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

  /* Per property, for the whole session: a decided card keeps its outcome (and
   * its undo) after the list re-reads it, on any page. */
  const [outcomes, setOutcomes] = useState<ReadonlyMap<number, SplitOutcome>>(new Map());
  const record = (o: SplitOutcome) => setOutcomes((prev) => new Map(prev).set(o.propertyId, o));

  const counts = { open: 0, split: 0, kept: 0 };
  for (const item of items) counts[cardState(item)] += 1;
  const missing = (page?.missing ?? []).filter((pid) => !outcomes.has(pid));

  return (
    <div className="px-6 pt-5 pb-10 max-w-screen-xl mx-auto">
      <header>
        <h1 className="text-2xl leading-tight">AUTODEDUP · Rozdělení podle kategorií</h1>
        <p className="mt-1 text-sm text-[var(--color-ink-2)] leading-relaxed max-w-[52rem]">
          Nemovitosti, ve kterých jsou inzeráty různých kategorií — třeba prodej a pronájem, nebo
          byt a dům. Takové inzeráty systém do jedné nemovitosti sám nikdy nespojí.
          Prohlédněte si fotky a texty a u každé nemovitosti rozhodněte: <strong>rozdělit podle
          písmen</strong> (každé další písmeno odejde jako samostatná nemovitost, inzerát se vrátí
          tam, odkud přišel, nebo dostane novou, a mezi písmeny se zapíše „různé“), nebo{' '}
          <strong>ponechat jako jednu nemovitost</strong> (mezi inzeráty se zapíše „stejné“). Písmena
          jsou předvyplněná podle kategorií a stejné písmeno znamená jednu nemovitost: inzerátu, který
          patří jinam, třeba jinému bytu, dejte jiné písmeno. Dokud nepotvrdíte, nic se nezmění.
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
          .filter((pid) => byId.has(pid) || outcomes.has(pid))
          .map((pid) => (
            <CategoryCard
              key={pid}
              propertyId={pid}
              item={byId.get(pid)}
              outcome={outcomes.get(pid)}
              onOutcome={record}
              member={member}
            />
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
  outcome,
  onOutcome,
  member,
}: {
  propertyId: number;
  item: CategorySplit | undefined;
  outcome: SplitOutcome | undefined;
  onOutcome: (o: SplitOutcome) => void;
  member: MemberOf;
}) {
  const qc = useQueryClient();
  const [armed, setArmed] = useState<Decision | null>(null);
  /* The letters hold for the ads they were set over: a re-read that changed the
   * ads starts again from the sides. */
  const [letters, setLetters] = useState<{ list: string; units: UnitMap } | null>(null);
  const run = useMutation({
    mutationFn: (statement: SplitStatement) =>
      sendSplit({
        propertyId,
        statement,
        sources: Object.fromEntries(
          (item?.groups ?? []).flatMap((g) => g.adverts).map((a) => [a.listing_id, a.source]),
        ),
      }),
    onSuccess: (o) => {
      setArmed(null);
      onOutcome(o);
      refreshAfterSplit(qc);
    },
  });
  const followUp = useMutation({
    mutationFn: followUpSplit,
    onSuccess: (o) => {
      onOutcome(o);
      refreshAfterSplit(qc);
    },
  });
  const busy = run.isPending || followUp.isPending;
  const said = outcome && (
    <SplitOutcomeBody
      outcome={outcome}
      busy={busy}
      onFollowUp={() => followUp.mutate(outcome)}
      summary={
        outcome.kind === 'ok' && outcome.statement.separate.length === 0 ? (
          <p>Ponecháno jako jedna nemovitost.</p>
        ) : undefined
      }
    />
  );

  if (outcome?.kind === 'ok') {
    return (
      <li
        data-testid={`category-${propertyId}`}
        className="rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-4 py-3"
      >
        <CardHeader propertyId={propertyId} />
        <div className="mt-2 text-[0.75rem]">{said}</div>
      </li>
    );
  }
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
        {said && <div className="mt-1 text-[0.75rem]">{said}</div>}
      </li>
    );
  }

  const ads = item.groups.flatMap((g) => g.adverts);
  const listKey = ads.map((a) => a.listing_id).sort((a, b) => a - b).join(',');
  const units = letters?.list === listKey ? letters.units : defaultLetters(item);
  const { plan, stuck } = letterPlan(item, units);
  const withheld = splitWithheld({ plan, stuck });
  const byId = new Map(ads.map((a) => [a.listing_id, a]));
  const portalOf = (id: number) => {
    const a = byId.get(id);
    return a ? (portalLabel(a.source) ?? a.source) : '';
  };
  const priceOf = (id: number) => {
    const a = byId.get(id);
    if (!a) return '';
    const m = member(a);
    return priceLabel(m.price_czk, m.category_type);
  };
  const lettering: Letters = {
    units,
    count: ads.filter((a) => !rides(a)).length,
    /* Set before the second click: what the confirm shows is what is sent. */
    disabled: busy || armed !== null,
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
      {said && <div className="mt-2 text-[0.75rem]">{said}</div>}

      {armed ? (
        <div
          role="group"
          aria-label={armed === 'split' ? 'Potvrdit rozdělení' : 'Potvrdit ponechání'}
          className="mt-3 rounded-[var(--radius-sm)] border border-[var(--color-brick)]/40 bg-[var(--color-brick-soft)] px-4 py-3"
        >
          {armed === 'split' ? (
            <div className="space-y-2">
              <SplitPlanLines
                plan={plan}
                propertyId={propertyId}
                portalOf={portalOf}
                priceOf={priceOf}
              />
            </div>
          ) : (
            <p className="text-[0.8rem] text-[var(--color-ink-2)]">{keepSentence(item)}</p>
          )}
          <div className="mt-2 flex items-center gap-2">
            <button
              type="button"
              autoFocus
              disabled={busy}
              onClick={() => run.mutate(armed === 'split' ? plan.statement : keepStatement(item))}
              className={brick}
            >
              {run.isPending ? 'Provádím…' : 'Potvrdit'}
            </button>
            <button type="button" disabled={busy} onClick={() => setArmed(null)} className={plain}>
              Zrušit
            </button>
          </div>
        </div>
      ) : (
        <>
          <p className="mt-3 text-[0.75rem] text-[var(--color-ink-2)] tabular-nums">
            {lettersLine(plan)}
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={busy || withheld != null}
              onClick={() => setArmed('split')}
              className={brick}
            >
              Rozdělit podle písmen
            </button>
            <button type="button" disabled={busy} onClick={() => setArmed('keep')} className={plain}>
              Ponechat jako jednu nemovitost
            </button>
            {withheld && (
              <span
                className={`text-[0.75rem] ${
                  stuck.length > 0 ? 'text-[var(--color-brick)]' : 'text-[var(--color-ink-3)]'
                }`}
              >
                {withheld}
              </span>
            )}
          </div>
        </>
      )}
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
          disabled={letters.disabled}
          onChange={(letter) => letters.onLetter(ad.listing_id, letter)}
          label={
            <>
              Nemovitost<span className="sr-only"> inzerátu {portal} #{ad.listing_id}</span>
            </>
          }
        />
      )}
      {saysUnmoved(ad) && (
        <p className={muted}>Nelze oddělit: {unmovedReason(ad.detach_outcome ?? '')}.</p>
      )}
      <MemberText title={ad.text.title} text={ad.text.description} label={`#${ad.listing_id}`} />
    </>
  );
}
