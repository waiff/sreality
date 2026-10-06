/* The letters as the split statement they make (`splitPlan`, E919), before
 * anything is written: one line per letter — the group that stays on the
 * property and the groups that leave, each ad named as its row names it
 * (portal and price) — and what the split records. The property page's split
 * panel and the category review's confirm say it in these words. */

import { Fragment } from 'react';

import { fmtCzk } from '@/lib/format';
import { stateStays, type SplitPlan } from '@/lib/mergedAdverts';

/* An ad's price as its row shows it; a rent is per month. */
export function priceLabel(price: number | null, categoryType: string | null | undefined): string {
  if (price == null) return 'cena neuvedena';
  return categoryType === 'pronajem' ? `${fmtCzk(price)} / měs` : fmtCzk(price);
}

export default function SplitPlanLines({
  plan,
  propertyId,
  portalOf,
  priceOf,
}: {
  plan: SplitPlan;
  propertyId: number;
  portalOf: (listingId: number) => string;
  priceOf: (listingId: number) => string;
}) {
  const groups = [plan.kept, ...plan.leaving].sort((a, b) => a.letter.localeCompare(b.letter));
  return (
    <>
      <ul className="space-y-0.5 text-[0.75rem] leading-snug text-[var(--color-ink-2)]">
        {groups.map((g) => (
          <li key={g.letter}>
            <span className="font-mono font-medium text-[var(--color-ink)]">{g.letter}</span> —{' '}
            {g === plan.kept ? (
              <>
                zůstává v nemovitosti <span className="font-mono tabular-nums">#{propertyId}</span>
              </>
            ) : (
              'odejde jako jedna nemovitost'
            )}
            :{' '}
            {g.listingIds.map((id, i) => (
              <Fragment key={id}>
                {i > 0 && ', '}
                <span className="text-[0.68rem] tracking-[0.06em] uppercase">{portalOf(id)}</span>{' '}
                <span className="font-mono tabular-nums" title={`inzerát #${id}`}>
                  {priceOf(id)}
                </span>
              </Fragment>
            ))}
          </li>
        ))}
      </ul>
      <p className="text-[0.72rem] leading-snug text-[var(--color-ink-2)]">
        Různá písmena = různé nemovitosti: každá dvojice inzerátů napříč písmeny se uloží jako
        „různé“ a dostane trvalý zákaz spojení; inzeráty se stejným písmenem zůstanou spolu jako
        jedna nemovitost. {stateStays(propertyId)}
      </p>
    </>
  );
}
