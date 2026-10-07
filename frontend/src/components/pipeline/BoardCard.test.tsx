/* The board card's broker tooltip.
 *
 * A non-admin session gets has_email / has_phone instead of the contact values
 * (the /brokers API masks per caller since 2026-08-12). Dropping the masked pair
 * silently would render exactly the same tooltip as a broker with no contact at
 * all — the honest-state gap this wave closes. */

import { describe, expect, it } from 'vitest';
import { brokerHoverTitle } from './BoardCard';
import type { ListingBroker } from '@/lib/brokers';

const broker = (over: Partial<ListingBroker>): ListingBroker => ({
  sreality_id: null,
  listing_id: 111,
  broker_id: 7,
  broker_display_name: 'Jan Novák',
  broker_firm_label: 'RE/MAX',
  ...over,
});

describe('brokerHoverTitle', () => {
  it('lists the real contact for an admin session', () => {
    expect(
      brokerHoverTitle(
        broker({ primary_phone: '+420 777 123 456', primary_email: 'jan@remax.cz' }),
      ),
    ).toBe('Jan Novák · RE/MAX · +420 777 123 456 · jan@remax.cz');
  });

  it('says the contact is admin-only when it is masked', () => {
    expect(brokerHoverTitle(broker({ has_phone: true }))).toBe(
      'Jan Novák · RE/MAX · kontakt jen pro adminy',
    );
  });

  it('stays silent about contact when the broker genuinely has none', () => {
    expect(brokerHoverTitle(broker({ has_email: false, has_phone: false }))).toBe(
      'Jan Novák · RE/MAX',
    );
  });

  it('falls back to the link label when nothing is known', () => {
    expect(
      brokerHoverTitle(broker({ broker_display_name: null, broker_firm_label: null })),
    ).toBe('Zobrazit makléře');
  });

  /* MS7: with no active ad the board shows the inactive ads' brokers, and says so. */
  it('says the broker comes from inactive ads', () => {
    expect(brokerHoverTitle(broker({}), true)).toBe(
      'Jan Novák · RE/MAX · z neaktivních inzerátů',
    );
  });
});
