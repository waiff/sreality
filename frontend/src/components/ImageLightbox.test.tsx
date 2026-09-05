/* ImageLightbox — the ONE full-screen photo modal (listing detail's gallery,
 * NEW DEDUP labeling's two review grids). Pins the contract they share:
 * keyboard nav + Escape, the scroll lock, whose tag the badge reports, and that
 * a grid shrinking underneath an open modal can't blank it out.
 *
 * Since W6b the Escape / focus / scroll-lock half of that contract is
 * lib/useDialog's, and is asserted through the shared expectDialogContract
 * (src/test/a11y.ts) rather than re-described here. What stays local is what is
 * bespoke about this viewer: the arrow keys, and the fact that pressing one
 * must NOT drag focus back to Close.
 */

import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import { expectDialogContract } from '@/test/a11y';
import ImageLightbox from './ImageLightbox';
import type { ImagePublic } from '@/lib/types';

const img = (id: number, tag: string | null): ImagePublic => ({
  id,
  sreality_id: 555,
  sequence: id,
  sreality_url: `https://example.test/${id}.jpg`,
  storage_path: null,
  clip_fine_tag: tag,
  clip_logical_tag: tag,
  clip_confidence: 0.5,
  clip_render_score: null,
  phash: null,
});

const IMAGES = [img(1, 'kitchen'), img(2, 'bathroom'), img(3, 'balcony')];

/* Mounting IS opening (lib/useDialog): the trigger renders the lightbox rather
 * than flipping an `open` prop on a mounted one, which is how every call site
 * uses it. */
function LightboxHost({ images = IMAGES }: { images?: ImagePublic[] }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        open-lightbox
      </button>
      {open && (
        <ImageLightbox images={images} startIndex={0} onClose={() => setOpen(false)} />
      )}
    </>
  );
}

describe('<ImageLightbox>', () => {
  it('walks the set with the arrow keys, wrapping at both ends', () => {
    render(<ImageLightbox images={IMAGES} startIndex={0} onClose={() => {}} />);
    expect(screen.getByText('1 / 3')).toBeInTheDocument();

    fireEvent.keyDown(document, { key: 'ArrowRight' });
    expect(screen.getByText('2 / 3')).toBeInTheDocument();

    fireEvent.keyDown(document, { key: 'ArrowLeft' });
    fireEvent.keyDown(document, { key: 'ArrowLeft' });
    expect(screen.getByText('3 / 3')).toBeInTheDocument();
  });

  it('closes on Escape and releases the page scroll lock', () => {
    const onClose = vi.fn();
    const { unmount } = render(
      <ImageLightbox images={IMAGES} startIndex={0} onClose={onClose} />,
    );
    expect(document.body.style.overflow).toBe('hidden');
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalled();
    unmount();
    expect(document.body.style.overflow).toBe('');
  });

  it('badges the photo with the image row\'s own CLIP tag by default', () => {
    render(<ImageLightbox images={IMAGES} startIndex={1} onClose={() => {}} />);
    expect(screen.getByText('koupelna')).toBeInTheDocument();
  });

  it('lets the caller override the tag per position, and follows navigation', () => {
    // The labeling grid badges a PROPOSED tag its images_public row knows
    // nothing about; the enlarged photo has to report the same thing.
    const proposed = ['interier - kuchyne', 'interier - koupelna', 'exterier - balkon'];
    render(
      <ImageLightbox
        images={IMAGES}
        startIndex={0}
        onClose={() => {}}
        tagAt={(i) => ({ tag: proposed[i], confidence: 0.9 })}
      />,
    );
    expect(screen.getByText('interier - kuchyne')).toBeInTheDocument();
    expect(screen.queryByText('kuchyně')).not.toBeInTheDocument();

    fireEvent.keyDown(document, { key: 'ArrowRight' });
    expect(screen.getByText('interier - koupelna')).toBeInTheDocument();
  });

  it('is named by the visible counter', () => {
    render(<ImageLightbox images={IMAGES} startIndex={0} onClose={() => {}} />);
    expect(screen.getByRole('dialog', { name: '1 / 3' })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'ArrowRight' });
    expect(screen.getByRole('dialog', { name: '2 / 3' })).toBeInTheDocument();
  });

  it('honours the whole modal-dialog contract', () => {
    // Focus in, Tab wrapping both ways, ONE layer per Escape, focus back on the
    // trigger, scroll lock released — all of it against the rendered DOM, from
    // the same helper every migrated dialog is held to.
    render(<LightboxHost />);
    const trigger = screen.getByRole('button', { name: 'open-lightbox' });
    expectDialogContract({ open: () => fireEvent.click(trigger), trigger });
  });

  it('leaves focus where the operator put it when an arrow key steps the set', () => {
    // THE FOCUS-THEFT BUG. The arrow handler used to share one effect with the
    // initial `closeBtn.focus()`, and that effect depended on [onClose, prev,
    // next] — prev/next change identity on every step — so each arrow press
    // re-ran it and slammed focus back onto Close. Initial focus is
    // useDialog's, mount-only; the arrow listener is its own effect now.
    render(<ImageLightbox images={IMAGES} startIndex={0} onClose={() => {}} />);
    const next = screen.getByRole('button', { name: 'Next photo' });
    next.focus();

    fireEvent.keyDown(document, { key: 'ArrowRight' });

    expect(screen.getByText('2 / 3')).toBeInTheDocument();
    expect(document.activeElement).toBe(next);
  });

  it('clamps the position when the set shrinks under an open modal', () => {
    // Reviewing a tile can drop it out of the grid the modal is a view of. An
    // unclamped index renders nothing at all — an invisible dialog still
    // holding the scroll lock.
    const { rerender } = render(
      <ImageLightbox images={IMAGES} startIndex={2} onClose={() => {}} />,
    );
    expect(screen.getByText('3 / 3')).toBeInTheDocument();

    rerender(
      <ImageLightbox images={IMAGES.slice(0, 2)} startIndex={2} onClose={() => {}} />,
    );
    expect(screen.getByText('2 / 2')).toBeInTheDocument();
    expect(screen.getByText('koupelna')).toBeInTheDocument();
  });
});
