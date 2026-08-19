/* The shared small/large photo-size switch (Browse's listing cards, the NEW
 * DEDUP labeling review grid). Presentation only — the flag and what "large"
 * does to a grid belong to the caller — so this pins exactly the contract the
 * two pages depend on: which segment reads as pressed, and what each click
 * reports.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import ImageSizeToggle from './ImageSizeToggle';

describe('<ImageSizeToggle>', () => {
  it('marks the active size pressed, and only that one', () => {
    const { rerender } = render(
      <ImageSizeToggle large={false} onChange={() => {}} label="Card image size" />,
    );
    expect(screen.getByRole('button', { name: /Small/ })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: /Large/ })).toHaveAttribute('aria-pressed', 'false');

    rerender(<ImageSizeToggle large onChange={() => {}} label="Card image size" />);
    expect(screen.getByRole('button', { name: /Small/ })).toHaveAttribute('aria-pressed', 'false');
    expect(screen.getByRole('button', { name: /Large/ })).toHaveAttribute('aria-pressed', 'true');
  });

  it('reports the size clicked, not a toggle — clicking the active one is a no-op change', () => {
    const onChange = vi.fn();
    render(<ImageSizeToggle large={false} onChange={onChange} label="Card image size" />);

    fireEvent.click(screen.getByRole('button', { name: /Large/ }));
    expect(onChange).toHaveBeenLastCalledWith(true);
    // Re-clicking the already-active segment must not flip back to large:
    // a segmented control sets a value, it doesn't toggle one.
    fireEvent.click(screen.getByRole('button', { name: /Small/ }));
    expect(onChange).toHaveBeenLastCalledWith(false);
  });

  it('names the group after whatever the calling surface sizes', () => {
    render(
      <ImageSizeToggle large={false} onChange={() => {}} label="Review grid image size" />,
    );
    // Each page has exactly one of these; the label is what tells a
    // screen-reader user which grid is about to be reshaped.
    expect(screen.getByRole('group', { name: 'Review grid image size' })).toBeInTheDocument();
  });
});
