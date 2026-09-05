import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import PriceDelta from './PriceDelta';

describe('<PriceDelta>', () => {
  /* THE point of the component. total_price_change_pct is NULL whenever the
   * representative listing has fewer than two priced snapshots — most of the
   * board — and that is not the same claim as "the price has not moved".
   * Rendering a flat arrow there would assert stability we never observed. */
  it('renders NOTHING when there is no price history', () => {
    const { container } = render(<PriceDelta pct={null} />);
    expect(container).toBeEmptyDOMElement();
    const { container: c2 } = render(<PriceDelta pct={undefined} />);
    expect(c2).toBeEmptyDOMElement();
  });

  it('renders a flat mark, not nothing, when the price was observed and never moved', () => {
    render(<PriceDelta pct={0} changes={0} />);
    expect(screen.getByTitle(/nezměnila/)).toBeInTheDocument();
  });

  it('distinguishes "moved and came back" from "never moved"', () => {
    render(<PriceDelta pct={0} changes={2} />);
    expect(screen.getByTitle(/vrátila/)).toBeInTheDocument();
    expect(screen.getByTitle(/2 změny ceny/)).toBeInTheDocument();
  });

  /* A cut is favourable in a buyer's pipeline — sage, not the stock-market red.
   * Matches the polarity ListingDetail's Stat has always used. */
  it('colours a drop sage and a rise brick', () => {
    const { container: drop } = render(<PriceDelta pct={-4.2} changes={1} />);
    expect(drop.querySelector('span')).toHaveStyle({ color: 'var(--color-sage)' });
    const { container: rise } = render(<PriceDelta pct={3.1} changes={1} />);
    expect(rise.querySelector('span')).toHaveStyle({ color: 'var(--color-brick)' });
  });

  /* The arrow carries the direction, so "↓ −4,2 %" would read as a double
   * negative. Magnitude only. */
  it('prints the magnitude without a sign', () => {
    render(<PriceDelta pct={-4.2} changes={1} />);
    expect(screen.getByText(/^4,2/)).toBeInTheDocument();
    expect(screen.queryByText(/-4,2/)).not.toBeInTheDocument();
  });

  /* The sentence belongs to the pointer, not to the accessibility tree. The chip
   * ships inside the Browse card <Link>, and an aria-label here would REPLACE
   * the visible "4,2 %" in that link's name with a whole Czech sentence. */
  it('describes the change in the title and carries no aria-label', () => {
    const { container } = render(<PriceDelta pct={-4.2} changes={1} />);
    const chip = container.querySelector('span')!;
    expect(chip).toHaveAttribute('title', expect.stringMatching(/Pokles.*-4,2/));
    expect(chip).not.toHaveAttribute('aria-label');
  });

  it('leaves an ancestor link named by the visible text', () => {
    render(
      <a href="#listing-900">
        Byt 2+kk
        <PriceDelta pct={-4.2} changes={1} />
      </a>,
    );
    const link = screen.getByRole('link');
    expect(link).toHaveAccessibleName(/^Byt 2\+kk/);
    expect(link).not.toHaveAccessibleName(/Pokles/);
  });
});
