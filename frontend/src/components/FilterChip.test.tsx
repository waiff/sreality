import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

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
});
