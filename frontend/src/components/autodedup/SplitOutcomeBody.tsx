/* AUTODEDUP · one split outcome in words (`splitOutcome.ts`): where each unit
 * sits now (a link), the server's undo ("Vrátit"), the E52 re-send ("Přesto
 * uložit") and what a refusal means. `summary` replaces the unit list when the
 * page has its own sentence for what was written. */

import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';

import type { SplitOutcome } from '@/components/autodedup/splitOutcome';
import { propertyPath } from '@/lib/listingUrl';
import { unitLanding } from '@/lib/mergedAdverts';

const linkClass = 'text-[var(--color-copper-2)] underline decoration-dotted underline-offset-2';
const button =
  'ml-2 rounded-[var(--radius-sm)] border border-[var(--color-rule)] px-2 py-0.5 text-[0.72rem] text-[var(--color-ink-2)] hover:bg-[var(--color-rule-soft)] disabled:opacity-50';

export default function SplitOutcomeBody({
  outcome: o,
  busy,
  onFollowUp,
  summary,
}: {
  outcome: SplitOutcome;
  busy: boolean;
  onFollowUp: () => void;
  summary?: ReactNode;
}) {
  const tag = (id: number) => `#${id}${o.sources[id] ? ` (${o.sources[id]})` : ''}`;
  if (o.kind === 'ok') {
    const r = o.result;
    return (
      <div className="text-[var(--color-ink-2)]">
        {summary ?? (
          <ul className="space-y-0.5">
            {r.units.map((u) => (
              <li key={u.unit}>
                {u.role === 'kept' ? 'zůstávají spolu' : 'odděleno'}
                {u.role === 'separated' && u.unit === r.record_kept_by
                  ? ' (drží záznam nemovitosti: poznámky, štítky, karta v pipeline)'
                  : ''}
                : {u.listing_ids.map(tag).join(', ')} →{' '}
                <Link to={propertyPath(u.property_id)} className={linkClass}>
                  {unitLanding(u, o.propertyId)}
                </Link>
              </li>
            ))}
          </ul>
        )}
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
