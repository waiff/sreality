/* MultiselectChips — toggle-style multi-value picker.
 *
 * Behaviour pinned:
 *   - all options render as PickButtons
 *   - clicking an unselected option adds it; clicking a selected one
 *     removes it
 *   - selected options carry aria-pressed=true
 *   - the onChange emits the full next array, not a delta
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import { MultiselectChips } from './MultiselectChips';

const OPTIONS = [
  { value: '2+kk', label: '2+kk' },
  { value: '2+1',  label: '2+1'  },
  { value: '3+kk', label: '3+kk' },
];

describe('<MultiselectChips>', () => {
  it('renders every option as a toggleable pill', () => {
    render(
      <MultiselectChips
        value={[]}
        options={OPTIONS}
        onChange={vi.fn()}
      />,
    );
    for (const opt of OPTIONS) {
      expect(screen.getByText(opt.label)).toBeInTheDocument();
    }
  });

  it('marks selected options with aria-pressed=true', () => {
    render(
      <MultiselectChips
        value={['2+kk']}
        options={OPTIONS}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByText('2+kk').closest('button'))
      .toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('2+1').closest('button'))
      .toHaveAttribute('aria-pressed', 'false');
  });

  it('adds an option to the value array on first click', () => {
    const onChange = vi.fn();
    render(
      <MultiselectChips
        value={[]}
        options={OPTIONS}
        onChange={onChange}
      />,
    );
    fireEvent.click(screen.getByText('2+1'));
    expect(onChange).toHaveBeenCalledWith(['2+1']);
  });

  it('removes an option on click when already selected', () => {
    const onChange = vi.fn();
    render(
      <MultiselectChips
        value={['2+kk', '2+1', '3+kk']}
        options={OPTIONS}
        onChange={onChange}
      />,
    );
    fireEvent.click(screen.getByText('2+1'));
    expect(onChange).toHaveBeenCalledWith(['2+kk', '3+kk']);
  });
});
