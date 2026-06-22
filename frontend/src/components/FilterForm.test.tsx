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

import { useState } from 'react';
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
        includeOnly={['category_type']}
        flat
      />,
    );
    // category_type is a PILL_GROUP with the four registry options;
    // at least one of them should appear as a button. (category_main is now
    // a multiselect scoped off the browse agenda, so it's no longer the
    // representative pill_group here.)
    expect(screen.getByRole('button', { name: 'Pronájem' })).toBeInTheDocument();
  });

  it('honours the includeOnly slice', () => {
    render(
      <FilterForm
        scope="browse"
        state={{}}
        onChange={vi.fn()}
        includeOnly={['category_type']}
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
        state={{ min_garden_area: 100, max_garden_area: 800 }}
        onChange={vi.fn()}
        includeOnly={['min_garden_area']}    // only one half listed
        flat
      />,
    );
    // garden area keeps the dual-thumb slider (range_slider + full
    // bounds). The slider exposes two range inputs; if pairing fired,
    // both exist.
    const sliders = screen.getAllByRole('slider');
    expect(sliders).toHaveLength(2);
  });

  it('renders a range_inputs filter as paired number inputs, not a slider', () => {
    render(
      <FilterForm
        scope="browse"
        state={{ min_price_czk: 5000, max_price_czk: 15_000 }}
        onChange={vi.fn()}
        includeOnly={['min_price_czk']}
        labels={{ min_price_czk: 'Price' }}
        flat
      />,
    );
    // price opts into range_inputs, so it renders plain number inputs
    // (NumberCell = type=text inputMode=numeric → role "textbox") even
    // though it still carries min/max/step bounds metadata.
    expect(screen.queryAllByRole('slider')).toHaveLength(0);
    expect(screen.getAllByRole('textbox').length).toBeGreaterThanOrEqual(2);
  });

  it("renders pill_group filters as <button aria-pressed>", () => {
    const onChange = vi.fn();
    render(
      <FilterForm
        scope="browse"
        state={{ category_type: 'pronajem' }}
        onChange={onChange}
        includeOnly={['category_type']}
        flat
      />,
    );
    const rent = screen.getByRole('button', { name: 'Pronájem' });
    expect(rent).toHaveAttribute('aria-pressed', 'true');
    const sale = screen.getByRole('button', { name: 'Prodej' });
    fireEvent.click(sale);
    // Single-filter updates ship as one-element arrays — the batching
    // shape is uniform across single + paired emissions so the parent
    // can always apply them atomically.
    expect(onChange).toHaveBeenCalledWith([
      { id: 'category_type', value: 'prodej' },
    ]);
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
    expect(onChange).toHaveBeenCalledWith([
      { id: 'has_balcony', value: true },
    ]);
  });

  it('batches paired range updates so min and max apply atomically', () => {
    // The original bug: dragging the lo thumb fired onChange(min, lo)
    // followed immediately by onChange(max, oldMax), and a non-
    // functional setter like Browse's URL writer saw both calls
    // against the same stale filters — second call won, lo never
    // changed. The batched shape sends both updates in one array so
    // the parent reduces them together.
    const onChange = vi.fn();
    render(
      <FilterForm
        scope="browse"
        state={{ min_garden_area: 1000, max_garden_area: 3000 }}
        onChange={onChange}
        includeOnly={['min_garden_area']}
        flat
      />,
    );
    const sliders = screen.getAllByRole('slider') as HTMLInputElement[];
    fireEvent.change(sliders[0], { target: { value: '2000' } });
    expect(onChange).toHaveBeenCalledTimes(1);
    const lastCall = onChange.mock.calls.at(-1)![0] as Array<{
      id: string; value: unknown;
    }>;
    expect(lastCall).toHaveLength(2);
    expect(lastCall.map((u) => u.id).sort()).toEqual([
      'max_garden_area', 'min_garden_area',
    ]);
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
        state={{ category_type: 'pronajem' }}
        onChange={vi.fn()}
        includeOnly={['category_type']}
        visibility={[
          {
            id: 'category_type',
            visibility: { browse: false, watchdog: true },
          },
        ]}
        flat
      />,
    );
    expect(screen.queryByRole('button', { name: 'Pronájem' })).not.toBeInTheDocument();
  });

  /* Stateful harness mirroring how Browse / WatchdogEdit feed updates
   * back into `state` — required to expose controlled-input round trips. */
  function StatefulNumberHarness({ id }: { id: string }) {
    const [state, setState] = useState<Record<string, unknown>>({ [id]: null });
    return (
      <FilterForm
        scope="browse"
        state={state}
        onChange={(updates) => {
          setState((prev) => {
            const next = { ...prev };
            for (const u of updates) next[u.id] = u.value;
            return next;
          });
        }}
        includeOnly={[id]}
        labels={{ [id]: 'signed' }}
        flat
      />
    );
  }

  it('keeps a typed leading minus alive in signed number inputs', () => {
    // Regression: the old parse-or-drop handler wiped the '-' keystroke
    // (controlled input restores prior value on a no-emit), so typing
    // "-10" into total_price_change_pct stored +10 — the opposite cohort.
    render(<StatefulNumberHarness id="total_price_change_pct" />);
    const input = screen.getByRole('textbox', { name: 'signed' });
    fireEvent.change(input, { target: { value: '-' } });
    expect(input).toHaveValue('-');
    fireEvent.change(input, { target: { value: '-1' } });
    fireEvent.change(input, { target: { value: '-10' } });
    expect(input).toHaveValue('-10');
  });

  it('keeps a mid-typing decimal point alive in float number inputs', () => {
    render(<StatefulNumberHarness id="total_price_change_pct" />);
    const input = screen.getByRole('textbox', { name: 'signed' });
    fireEvent.change(input, { target: { value: '7' } });
    fireEvent.change(input, { target: { value: '7.' } });
    expect(input).toHaveValue('7.');
    fireEvent.change(input, { target: { value: '7.5' } });
    expect(input).toHaveValue('7.5');
  });
});
