/* The three external-map chips under the header map. Cheap surface, but the
   parts that silently rot are the ones pinned here: that all three render, that
   each one opens in a new tab without leaking the referrer, and that the hrefs
   carry THIS listing's coordinate rather than a default view of Czechia. */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import ExternalMapLinks from './ExternalMapLinks';

describe('<ExternalMapLinks>', () => {
  it('renders one external link per service, pointed at the given coordinate', () => {
    render(<ExternalMapLinks lat={50.081234} lng={14.428765} />);

    expect(screen.getByRole('link', { name: /Mapy\.cz/ })).toHaveAttribute(
      'href',
      'https://mapy.com/fnc/v1/showmap?center=14.428765,50.081234&zoom=17&marker=true',
    );
    expect(screen.getByRole('link', { name: /Google/ })).toHaveAttribute(
      'href',
      'https://www.google.com/maps/search/?api=1&query=50.081234,14.428765',
    );
    expect(screen.getByRole('link', { name: /Katastr/ })).toHaveAttribute(
      'href',
      'https://ikatastr.cz/#kde=50.081234,14.428765,18&mapa=zakladni' +
        '&vrstvy=parcelybudovy&info=50.081234,14.428765',
    );
  });

  it('opens every link in a new tab, with the noopener/noreferrer pair', () => {
    render(<ExternalMapLinks lat={50} lng={14} />);

    const links = screen.getAllByRole('link');
    expect(links).toHaveLength(3);
    for (const a of links) {
      expect(a).toHaveAttribute('target', '_blank');
      expect(a.getAttribute('rel')).toBe('noopener noreferrer');
      // The hover text is the only place the full service name and its purpose
      // live — the chip label is abbreviated to fit the map column.
      expect(a.getAttribute('title')).toBeTruthy();
    }
  });
});
