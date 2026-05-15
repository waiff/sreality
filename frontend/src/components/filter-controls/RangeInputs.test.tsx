/* RangeInputs — paired min/max number inputs.
 *
 * Behaviour pinned here:
 *   - empty input emits null on that side, keeping the other side stable
 *   - non-finite text is ignored (no NaN leaking through)
 *   - coerce='int' truncates fractional values
 *   - aria-labels surface the field for accessibility tests
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import { RangeInputs } from './RangeInputs';

describe('<RangeInputs>', () => {
  it('renders the current min and max values', () => {
    render(
      <RangeInputs
        minValue={1000}
        maxValue={5000}
        onChange={vi.fn()}
        ariaLabelMin="price min"
        ariaLabelMax="price max"
      />,
    );
    const min = screen.getByLabelText('price min') as HTMLInputElement;
    const max = screen.getByLabelText('price max') as HTMLInputElement;
    expect(min.value).toBe('1000');
    expect(max.value).toBe('5000');
  });

  it('emits null on the cleared side and preserves the other', () => {
    const onChange = vi.fn();
    render(
      <RangeInputs
        minValue={1000}
        maxValue={5000}
        onChange={onChange}
        ariaLabelMin="min"
        ariaLabelMax="max"
      />,
    );
    fireEvent.change(screen.getByLabelText('min'), {
      target: { value: '' },
    });
    expect(onChange).toHaveBeenCalledWith(null, 5000);
  });

  it('coerces float input when no coerce option is set', () => {
    const onChange = vi.fn();
    render(
      <RangeInputs
        minValue={null}
        maxValue={null}
        onChange={onChange}
        ariaLabelMin="min"
        ariaLabelMax="max"
      />,
    );
    fireEvent.change(screen.getByLabelText('max'), {
      target: { value: '40.5' },
    });
    expect(onChange).toHaveBeenLastCalledWith(null, 40.5);
  });

  it("truncates to integers when coerce='int'", () => {
    const onChange = vi.fn();
    render(
      <RangeInputs
        minValue={null}
        maxValue={null}
        coerce="int"
        onChange={onChange}
        ariaLabelMin="min"
        ariaLabelMax="max"
      />,
    );
    fireEvent.change(screen.getByLabelText('min'), {
      target: { value: '40.9' },
    });
    expect(onChange).toHaveBeenLastCalledWith(40, null);
  });

  it('ignores non-numeric input rather than emitting NaN', () => {
    const onChange = vi.fn();
    render(
      <RangeInputs
        minValue={100}
        maxValue={null}
        onChange={onChange}
        ariaLabelMin="min"
        ariaLabelMax="max"
      />,
    );
    fireEvent.change(screen.getByLabelText('min'), {
      target: { value: 'abc' },
    });
    // Garbage in → null on that side; the other side stays where it was.
    expect(onChange).toHaveBeenLastCalledWith(null, null);
  });
});
