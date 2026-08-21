/* BorderCaseButton — the shared quarantine toggle rendered by /clip-audit's
 * TrainControl and the NEW DEDUP Labeling review tile.
 *
 * Presentational: it reads and calls the store, nothing else. Pins the two
 * states, that a click delegates with THIS tile's image id, and that an
 * in-flight write locks the button (so a double click can't race a flag against
 * its own unflag). The store's own behavior is useBorderCases.test.tsx.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import BorderCaseButton from './BorderCaseButton';
import type { BorderCaseStore } from '@/lib/useBorderCases';

const store = (over: Partial<BorderCaseStore> = {}): BorderCaseStore => ({
  has: () => false,
  isPending: () => false,
  toggle: vi.fn(),
  ...over,
});

describe('<BorderCaseButton>', () => {
  it('reads unflagged as the plain label', () => {
    render(<BorderCaseButton imageId={7} store={store()} />);
    const btn = screen.getByRole('button', { name: 'Border case' });
    expect(btn).toHaveAttribute('aria-pressed', 'false');
  });

  it('reads flagged as the checked label', () => {
    render(<BorderCaseButton imageId={7} store={store({ has: () => true })} />);
    const btn = screen.getByRole('button', { name: '✓ Border case' });
    expect(btn).toHaveAttribute('aria-pressed', 'true');
  });

  it('delegates the click to the store with its own image id', () => {
    const s = store();
    render(<BorderCaseButton imageId={7} store={s} />);
    fireEvent.click(screen.getByRole('button'));
    expect(s.toggle).toHaveBeenCalledWith(7);
  });

  it('locks while a write for that image is in flight', () => {
    const s = store({ isPending: () => true });
    render(<BorderCaseButton imageId={7} store={s} />);
    const btn = screen.getByRole('button');
    expect(btn).toBeDisabled();
    fireEvent.click(btn);
    expect(s.toggle).not.toHaveBeenCalled();
  });
});
