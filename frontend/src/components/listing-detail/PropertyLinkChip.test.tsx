/* PropertyLinkChip — the listing page's one link out to its parent property. */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import { PropertyLinkChip } from './PropertyLinkChip';

describe('<PropertyLinkChip>', () => {
  it('renders nothing when the property id has not resolved yet', () => {
    const { container } = render(
      <MemoryRouter>
        <PropertyLinkChip propertyId={null} sourceCount={1} />
      </MemoryRouter>,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('links to /property/:id and shows the source count for a multi-portal property', () => {
    render(
      <MemoryRouter>
        <PropertyLinkChip propertyId={3309} sourceCount={2} />
      </MemoryRouter>,
    );
    const link = screen.getByRole('link');
    expect(link).toHaveAttribute('href', '/property/3309');
    expect(link).toHaveTextContent('#3309');
    expect(link).toHaveTextContent('2×');
  });

  it('omits the source count for a singleton property', () => {
    render(
      <MemoryRouter>
        <PropertyLinkChip propertyId={7} sourceCount={1} />
      </MemoryRouter>,
    );
    const link = screen.getByRole('link');
    expect(link).toHaveTextContent('#7');
    expect(link).not.toHaveTextContent('×');
  });
});
