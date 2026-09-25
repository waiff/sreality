/* ListingOverview — the vertical order of the dossier sections. The operator's
 * own curation (collections / tags / notes) sits directly under the description
 * and above the estimates, which sit above the photos. The key facts live in the
 * header's price column, not as their own row between header and description. */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import { ListingOverview } from './ListingOverview';
import type { ListingPublic } from '@/lib/types';

const LISTING = {
  id: 1,
  sreality_id: 1,
  is_active: true,
  first_seen_at: '2026-01-01T00:00:00Z',
  last_seen_at: '2026-01-02T00:00:00Z',
  source: 'sreality',
  category_main: 'byt',
  category_type: 'prodej',
  price_czk: 5_000_000,
  disposition: '2+kk',
  description: 'Světlý byt po rekonstrukci.',
  building_type: 'cihlova',
} as unknown as ListingPublic;

function precedes(a: HTMLElement, b: HTMLElement): boolean {
  return Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
}

describe('<ListingOverview> section order', () => {
  it('renders the key facts in the price column, ahead of the description', () => {
    render(
      <MemoryRouter>
        <ListingOverview listing={LISTING} />
      </MemoryRouter>,
    );

    const price = screen.getByRole('heading', { level: 1 });
    const facts = screen.getByText('Building');
    const description = screen.getByText('Description');

    expect(price.parentElement).toContainElement(facts);
    expect(precedes(price, facts)).toBe(true);
    expect(precedes(facts, description)).toBe(true);
  });

  it('renders description, then curation, then estimates, then photos', () => {
    render(
      <MemoryRouter>
        <ListingOverview
          listing={LISTING}
          curationSlot={<div data-testid="curation" />}
          estimatesSlot={<div data-testid="estimates" />}
        />
      </MemoryRouter>,
    );

    const description = screen.getByText('Description');
    const curation = screen.getByTestId('curation');
    const estimates = screen.getByTestId('estimates');
    const photos = screen.getByText('Photos');

    expect(precedes(description, curation)).toBe(true);
    expect(precedes(curation, estimates)).toBe(true);
    expect(precedes(estimates, photos)).toBe(true);
  });

  it('renders no curation chrome when the slot is empty', () => {
    render(
      <MemoryRouter>
        <ListingOverview listing={LISTING} estimatesSlot={<div data-testid="estimates" />} />
      </MemoryRouter>,
    );

    expect(screen.queryByTestId('curation')).not.toBeInTheDocument();
    expect(screen.getByTestId('estimates')).toBeInTheDocument();
  });
});
