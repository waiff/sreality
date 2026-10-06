/* AUTODEDUP · the parts every split card shares (the proposals page and the
 * category review): the property's links and the reason line per split pair,
 * with the operator's newest ruling on it. */

import { Link } from 'react-router-dom';

import { VERDICT_LABELS, displayVerdict } from '@/components/autodedup/VerdictButtons';
import type { AutodedupVerdictValue, ProposedSplit } from '@/lib/api';
import { propertyPath } from '@/lib/listingUrl';
import { ROUTES, withQuery } from '@/lib/routes';

const REASON_SOURCE: Record<ProposedSplit['splits'][number]['reason_source'], string> = {
  conflict: 'konflikt',
  pair: 'dvojice',
  must_not_link: 'zákaz sloučení',
  none: 'bez uvedeného důvodu',
  not_compared: 'neporovnáno',
  category: 'kategorie',
};

const linkClass =
  'text-[0.8rem] text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2';

/* The property page, and the operator's rulings on its ads (E920). */
export function PropertyLinks({ propertyId }: { propertyId: number }) {
  return (
    <>
      <Link to={propertyPath(propertyId)} className={linkClass}>
        detail
      </Link>
      <Link
        to={withQuery(ROUTES.autodedupRulings.build(), { property: propertyId })}
        className={linkClass}
      >
        Rozhodnutí o těchto inzerátech
      </Link>
    </>
  );
}

export function SplitReasons({ splits }: { splits: ProposedSplit['splits'] }) {
  return (
    <ul className="mt-3 space-y-0.5 text-[0.75rem] text-[var(--color-ink-2)]">
      {splits.map((s) => (
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
  );
}
