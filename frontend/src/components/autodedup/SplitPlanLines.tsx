/* The split's preview in words (MS18), as the server read it: one line per
 * letter — where it lands, each ad named as its row names it (portal and price),
 * whether its landings are merged into one, and why rule 15 refuses that — then
 * what the split rules, and what a letter's join rules as the merge it is (MS12:
 * "Stejné" between the merged properties' main ads, and the "Různé" it takes
 * back). The property page's split dialog says it in these words. */

import { Fragment } from 'react';

import type { SplitPreview, SplitPreviewLetter } from '@/lib/api';
import { czPlural, fmtCzk } from '@/lib/format';

/* An ad's price as its row shows it; a rent is per month. */
export function priceLabel(price: number | null, categoryType: string | null | undefined): string {
  if (price == null) return 'cena neuvedena';
  return categoryType === 'pronajem' ? `${fmtCzk(price)} / měs` : fmtCzk(price);
}

const dvojic = (n: number) => czPlural(n, 'dvojice', 'dvojic', 'dvojic');

function lands(l: SplitPreviewLetter, propertyId: number): string {
  if (l.lands === 'kept') return `zůstává v nemovitosti #${l.property_id ?? propertyId}`;
  if (l.lands === 'origin') return `vrátí se do nemovitosti #${l.property_id}`;
  return 'odejde jako nová nemovitost';
}

function joins(l: SplitPreviewLetter): string {
  if (l.joins < 2) return '';
  const into = l.lands === 'new' ? 'do nové' : `do #${l.property_id}`;
  return ` (sloučí se z ${l.joins} ${czPlural(l.joins, 'nemovitosti', 'nemovitostí', 'nemovitostí')} ${into})`;
}

export default function SplitPlanLines({
  preview,
  portalOf,
  priceOf,
}: {
  preview: SplitPreview;
  portalOf: (listingId: number) => string;
  priceOf: (listingId: number) => string;
}) {
  const { rulings } = preview;
  const joined = preview.letters.filter((l) => l.joins >= 2);
  const takenBack = (letter: string) =>
    rulings.inside
      .filter((x) => x.letter === letter && x.taken_back)
      .reduce((n, x) => n + x.pairs.length + x.sets, 0);
  return (
    <>
      <ul className="space-y-0.5 text-[0.75rem] leading-snug text-[var(--color-ink-2)]">
        {preview.letters.map((l) => (
          <li key={l.letter}>
            <span className="font-mono font-medium text-[var(--color-ink)]">{l.letter}</span> —{' '}
            {lands(l, preview.property_id)}
            {joins(l)}:{' '}
            {l.listing_ids.map((id, i) => (
              <Fragment key={id}>
                {i > 0 && ', '}
                <span className="text-[0.68rem] tracking-[0.06em] uppercase">{portalOf(id)}</span>{' '}
                <span className="font-mono tabular-nums" title={`inzerát #${id}`}>
                  {priceOf(id)}
                </span>
              </Fragment>
            ))}
            {l.refused && (
              <p role="alert" className="text-[var(--color-brick)]">
                {l.refused.message}
              </p>
            )}
          </li>
        ))}
      </ul>
      <ul className="space-y-0.5 text-[0.72rem] leading-snug text-[var(--color-ink-2)]">
        <li>
          Mezi písmeny se zapíše „Různé“ u {rulings.different} {dvojic(rulings.different)} s
          trvalým zákazem spojení
          {joined.length === 0 ? '; mezi inzeráty se stejným písmenem se nic nezapíše.' : '.'}
        </li>
        {joined.map((l) => {
          const n = takenBack(l.letter);
          return (
            <li key={`${l.letter}-joined`}>
              Sloučení písmene {l.letter} zapíše „Stejné“ mezi hlavními inzeráty sloučených
              nemovitostí (jako každé sloučení)
              {n > 0 ? ` a vezme zpět ${n} rozhodnutí „Různé“.` : '.'}
            </li>
          );
        })}
        {rulings.inside
          .filter((x) => !x.taken_back)
          .map((x) => (
            <li key={`${x.letter}-stands`}>
              V písmenu {x.letter} platí „Různé“ ({x.pairs.length + x.sets}); rozdělení ho nezmění.
            </li>
          ))}
      </ul>
    </>
  );
}
