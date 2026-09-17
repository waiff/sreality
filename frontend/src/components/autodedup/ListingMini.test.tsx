/* ListingMini — one advert as every AUTODEDUP validation surface shows it.
 *
 * The pin: a photo the portal refuses is a LABELLED tile, never a blank box.
 * The operator is being asked to judge whether two adverts are the same flat,
 * and "this advert has no photos" is a fact about the advert while "iDNES would
 * not serve us this photo" is a fact about us. The card gallery has to say the
 * second one as plainly as the single cover always did.
 */

import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import ListingMini from './ListingMini';
import type { AutodedupMember } from '@/lib/api';

const member = (over: Partial<AutodedupMember> = {}): AutodedupMember => ({
  listing_id: 101,
  source: 'idnes',
  source_url: 'https://reality.idnes.cz/detail/1',
  category_main: 'byt',
  category_type: 'prodej',
  disposition: '2+kk',
  area_m2: 54,
  floor: 3,
  price_czk: 5_900_000,
  first_seen_at: '2026-01-04T00:00:00Z',
  last_seen_at: '2026-03-01T00:00:00Z',
  is_active: true,
  cover: { storage_path: null, sreality_url: 'https://img.example.invalid/a.jpg' },
  n_images: 30,
  ...over,
});

const show = (m: AutodedupMember) =>
  render(
    <MemoryRouter>
      <ListingMini member={m} />
    </MemoryRouter>,
  );

describe('<ListingMini>', () => {
  it('names the portal when a GALLERY frame will not load', () => {
    show(
      member({
        images: [
          { storage_path: null, sreality_url: 'https://img.example.invalid/1.jpg', sequence: 1, image_id: 1 },
          { storage_path: null, sreality_url: 'https://img.example.invalid/2.jpg', sequence: 2, image_id: 2 },
        ],
      }),
    );
    fireEvent.error(document.querySelector('img')!);
    /* Not an empty inset box under a "+28 fotek v detailu" badge. */
    expect(screen.getByText('foto nedostupné')).toBeInTheDocument();
    expect(screen.getAllByText(/idnes/i).length).toBeGreaterThan(0);
  });

  it('still names it when there is only a cover', () => {
    show(member());
    fireEvent.error(document.querySelector('img')!);
    expect(screen.getByText('foto nedostupné')).toBeInTheDocument();
  });

  it('says "bez fota" — a different fact — when the advert carries none', () => {
    show(member({ cover: null, n_images: 0 }));
    expect(screen.getByText('bez fota')).toBeInTheDocument();
  });
});
