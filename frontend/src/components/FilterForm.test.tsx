/* FilterForm — registry-driven widget dispatch.
 *
 * Focused on the routing decisions FilterForm makes:
 *   - filters declared for the requested agenda render; others don't
 *   - includeOnly narrows the set, with min/max pair auto-inclusion
 *   - tri-state filters render as TriRow (any/yes/no)
 *   - pill_group filters render as a button grid
 *   - paired ranges with full bounds render the slider; without
 *     bounds, they render paired inputs
 *   - customWidgets overrides the dispatcher
 *   - visibility[scope] = false hides a filter even when scoped in
 *
 * Skips the composite LOCATION control — it pulls maplibre-gl, which
 * doesn't initialise cleanly in jsdom and isn't part of the FilterForm
 * dispatch logic anyway.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import { FilterForm } from './FilterForm';

describe('<FilterForm>', () => {
  it('renders filters declared for the scope', () => {
    render(
      <FilterForm
        scope="browse"
        state={{}}
        onChange={vi.fn()}
        includeOnly={['category_main']}
        flat
      />,
    );
    // category_main is a PILL_GROUP with the four registry options;
    // at least one of them should appear as a button.
    expect(screen.getByRole('button', { name: 'Byty' })).toBeInTheDocument();
  });

  it('honours the includeOnly slice', () => {
    render(
      <FilterForm
        scope="browse"
        state={{}}
        onChange={vi.fn()}
        includeOnly={['category_main']}
        flat
      />,
    );
    // dispositions has 10 pills; none should render when excluded.
    expect(screen.queryByRole('button', { name: '2+kk' })).not.toBeInTheDocument();
  });

  it('auto-pairs min and max siblings on a slider filter', () => {
    render(
      <FilterForm
        scope="browse"
        state={{ min_price_czk: 5000, max_price_czk: 15_000 }}
        onChange={vi.fn()}
        includeOnly={['min_price_czk']}      // only one half listed
        flat
      />,
    );
    // The dual-thumb slider exposes two range inputs ("Price minimum"
    // and "Price maximum"). If pairing fired, both inputs exist.
    const sliders = screen.getAllByRole('slider');
    expect(sliders).toHaveLength(2);
  });

  it("renders pill_group filters as <button aria-pressed>", () => {
    const onChange = vi.fn();
    render(
      <FilterForm
        scope="browse"
        state={{ category_main: 'byt' }}
        onChange={onChange}
        includeOnly={['category_main']}
        flat
      />,
    );
    const byt = screen.getByRole('button', { name: 'Byty' });
    expect(byt).toHaveAttribute('aria-pressed', 'true');
    const dum = screen.getByRole('button', { name: 'Domy' });
    fireEvent.click(dum);
    expect(onChange).toHaveBeenCalledWith('category_main', 'dum');
  });

  it('renders tristate filters as the TriRow control', () => {
    const onChange = vi.fn();
    render(
      <FilterForm
        scope="browse"
        state={{ has_balcony: null }}
        onChange={onChange}
        includeOnly={['has_balcony']}
        labels={{ has_balcony: 'Balcony' }}
        flat
      />,
    );
    expect(screen.getByText('Balcony')).toBeInTheDocument();
    // TriRow exposes "any", "yes", "no" buttons.
    const yes = screen.getByRole('button', { name: 'yes' });
    fireEvent.click(yes);
    expect(onChange).toHaveBeenCalledWith('has_balcony', true);
  });

  it('routes a custom widget through the customWidgets prop', () => {
    const CustomWidget = vi.fn(() => <div>custom-rendered</div>);
    render(
      <FilterForm
        scope="browse"
        state={{ tags: null }}
        onChange={vi.fn()}
        includeOnly={['tags']}
        customWidgets={{ tags: CustomWidget }}
        flat
      />,
    );
    expect(screen.getByText('custom-rendered')).toBeInTheDocument();
    expect(CustomWidget).toHaveBeenCalled();
  });

  it('hides filters where visibility[scope] is false', () => {
    render(
      <FilterForm
        scope="browse"
        state={{ category_main: 'byt' }}
        onChange={vi.fn()}
        includeOnly={['category_main']}
        visibility={[
          {
            id: 'category_main',
            visibility: { browse: false, watchdog: true },
          },
        ]}
        flat
      />,
    );
    expect(screen.queryByRole('button', { name: 'Byty' })).not.toBeInTheDocument();
  });
});
