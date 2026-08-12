/* The board card's broker tooltip.
 *
 * A non-admin session gets has_email / has_phone instead of the contact values
 * (the /brokers API masks per caller since 2026-08-12). Dropping the masked pair
 * silently would render exactly the same tooltip as a broker with no contact at
 * all — the honest-state gap this wave closes. */

import { describe, expect, it } from 'vitest';
import { brokerHoverTitle } from './BoardCard';
import type { PipelineCardBroker } from '@/lib/types';

const broker = (over: Partial<PipelineCardBroker>): PipelineCardBroker => ({
  broker_id: 7,
  display_name: 'Jan Novák',
  firm_label: 'RE/MAX',
  email: null,
  phone: null,
  has_email: false,
  has_phone: false,
  ...over,
});

describe('brokerHoverTitle', () => {
  it('lists the real contact for an admin session', () => {
    expect(
      brokerHoverTitle(
        broker({ phone: '+420 777 123 456', email: 'jan@remax.cz', has_phone: true, has_email: true }),
      ),
    ).toBe('Jan Novák · RE/MAX · +420 777 123 456 · jan@remax.cz');
  });

  it('says the contact is admin-only when it is masked', () => {
    expect(brokerHoverTitle(broker({ has_phone: true }))).toBe(
      'Jan Novák · RE/MAX · kontakt jen pro adminy',
    );
  });

  it('stays silent about contact when the broker genuinely has none', () => {
    expect(brokerHoverTitle(broker({}))).toBe('Jan Novák · RE/MAX');
  });

  it('falls back to the link label when nothing is known', () => {
    expect(brokerHoverTitle(broker({ display_name: null, firm_label: null }))).toBe(
      'Zobrazit makléře',
    );
  });
});
