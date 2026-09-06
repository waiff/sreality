/* The comparable's photo strip. The carousel's prev/next buttons have always
 * been named; the six thumbnails under them were not — their only child is an
 * <img alt="">, so each one reached the accessibility tree as an unnamed
 * button.
 *
 * Since W6b this renders through <Dialog>, so the modal half of its contract —
 * focus in, Tab containment, one layer per Escape, focus restored, scroll lock
 * released — is asserted through the shared expectDialogContract
 * (src/test/a11y.ts). Its behaviour as the INNER half of the app's one nested
 * pair is proved where the pair actually lives: RunDetailModal.test.tsx. */

import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import { expectDialogContract } from '@/test/a11y';
import ComparableModal from './ComparableModal';
import type { ImagePublic, ListingPublic } from '@/lib/types';

vi.mock('@/lib/imageUrl', () => ({ imageSrc: () => 'blob:photo' }));

const LISTING = {
  id: 1,
  sreality_id: 900,
  source: 'sreality',
  source_id_native: '900',
  title: 'Byt 2+kk',
  price_czk: 5_400_000,
  area_m2: 62,
  price_per_m2: 87_096,
  price_per_m2_basis: 'sale_floor_area',
  category_main: 'byt',
  category_type: 'prodej',
  is_active: true,
} as unknown as ListingPublic;

const photo = (id: number): ImagePublic =>
  ({ id, sreality_url: `https://img/${id}.jpg`, storage_path: null }) as unknown as ImagePublic;

function renderModal(images: ImagePublic[]) {
  return render(
    <MemoryRouter>
      <ComparableModal
        listing={LISTING}
        images={images}
        summary={null}
        summaryError={null}
        summaryLoading={false}
        onClose={() => {}}
      />
    </MemoryRouter>,
  );
}

/* Mounting IS opening (lib/useDialog), which is how the comparables table
 * opens it: `{activeId != null && <ComparableModal/>}`. */
function ModalHost() {
  const [open, setOpen] = useState(false);
  return (
    <MemoryRouter>
      <button type="button" onClick={() => setOpen(true)}>
        open-comparable
      </button>
      {open && (
        <ComparableModal
          listing={LISTING}
          images={[photo(1), photo(2)]}
          summary={null}
          summaryError={null}
          summaryLoading={false}
          onClose={() => setOpen(false)}
        />
      )}
    </MemoryRouter>
  );
}

describe('<ComparableModal> as a dialog', () => {
  it('honours the whole modal-dialog contract', () => {
    render(<ModalHost />);
    const trigger = screen.getByRole('button', { name: 'open-comparable' });
    expectDialogContract({ open: () => fireEvent.click(trigger), trigger });
  });

  it('is named by the line the operator reads at the top of it', () => {
    // aria-labelledby onto the visible eyebrow, not a literal that could drift
    // away from the words on screen.
    renderModal([photo(1)]);
    expect(screen.getByRole('dialog')).toHaveAccessibleName('Comparable · id 900');
  });
});

describe('<ComparableModal> photo thumbnails', () => {
  it('names every thumbnail by its position in the strip', () => {
    renderModal([photo(1), photo(2), photo(3)]);
    expect(screen.getByRole('button', { name: 'Photo 1' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Photo 3' })).toBeInTheDocument();
  });

  it('leaves no unnamed button in the strip', () => {
    renderModal([photo(1), photo(2)]);
    for (const b of screen.getAllByRole('button')) {
      expect(b).not.toHaveAccessibleName('');
    }
  });
});
