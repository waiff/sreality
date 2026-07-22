import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import FilterChip from './FilterChip';

describe('<FilterChip>', () => {
  it('renders just the label when count is omitted', () => {
    render(<FilterChip on={false} label="kuchyně" onClick={vi.fn()} />);
    expect(screen.getByRole('button')).toHaveTextContent('kuchyně');
  });

  it('renders a trailing count badge when count is given', () => {
    render(<FilterChip on={false} label="kuchyně" count={12} onClick={vi.fn()} />);
    expect(screen.getByRole('button')).toHaveTextContent('kuchyně12');
  });

  it('still shows a zero count explicitly (not falsy-hidden)', () => {
    render(<FilterChip on={false} label="ostatní" count={0} onClick={vi.fn()} />);
    expect(screen.getByRole('button')).toHaveTextContent('ostatní0');
  });

  it('stays a single button when onRemove is omitted', () => {
    render(<FilterChip on={false} label="kuchyně" count={3} onClick={vi.fn()} />);
    expect(screen.getAllByRole('button')).toHaveLength(1);
  });

  it('with onRemove: splits into toggle + trash, each firing only its own handler', () => {
    const onClick = vi.fn();
    const onRemove = vi.fn();
    render(
      <FilterChip
        on={false}
        label="kuchyně"
        count={3}
        onClick={onClick}
        onRemove={onRemove}
        removeLabel="Odebrat kuchyně"
      />,
    );
    expect(screen.getAllByRole('button')).toHaveLength(2);
    fireEvent.click(screen.getByRole('button', { name: 'Odebrat kuchyně' }));
    expect(onRemove).toHaveBeenCalledTimes(1);
    expect(onClick).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'kuchyně 3' }));
    expect(onClick).toHaveBeenCalledTimes(1);
    expect(onRemove).toHaveBeenCalledTimes(1);
  });
});
