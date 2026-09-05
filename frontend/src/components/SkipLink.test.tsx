/* The first tab stop on every page (WCAG 2.4.1). */
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import SkipLink from './SkipLink';
import { MAIN_ID } from '@/lib/useRouteFocus';

describe('<SkipLink>', () => {
  it('is a real link to the main landmark', () => {
    render(
      <>
        <SkipLink />
        <nav>
          <a href="#x">nav item</a>
        </nav>
        <main id={MAIN_ID} tabIndex={-1} />
      </>,
    );
    const link = screen.getByRole('link', { name: 'Skip to content' });
    expect(link).toHaveAttribute('href', `#${MAIN_ID}`);
  });

  it('comes first in document order, so it is the first Tab stop', () => {
    render(
      <>
        <SkipLink />
        <nav>
          <a href="#x">nav item</a>
        </nav>
      </>,
    );
    const links = screen.getAllByRole('link');
    expect(links[0]).toHaveTextContent('Skip to content');
  });

  it('is visually hidden until focused, never removed from the tree', () => {
    render(<SkipLink />);
    const link = screen.getByRole('link', { name: 'Skip to content' });
    // sr-only keeps it in the accessibility tree; the focus: variants reveal it.
    expect(link.className).toContain('sr-only');
    expect(link.className).toContain('focus:not-sr-only');
  });
});
