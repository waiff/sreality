/* The Pipeline chip loads a VIEW, and both halves of that have to hold in the
 * URL adapter — the one Browse actually runs on:
 *   1. the cohort is reset to neutral (the shipped bug: the chip ANDed the
 *      scope onto byt+pronájem, hiding 44 of 45 deals), and
 *   2. any active preset is deselected IN THE SAME WRITE. Two writes against
 *      one searchParams snapshot would clobber each other, and a preset left
 *      marked active over replaced filters reads as dirty — popping the
 *      "Update preset" button this chip must never show.
 */

import { describe, expect, it } from 'vitest';
import { act, render } from '@testing-library/react';
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom';

import { useUrlBrowseState, type BrowseViewState } from './browseState';
import { fromSearchParams, pipelineViewFilters } from './filters';

function Harness({
  onReady,
}: {
  onReady: (s: BrowseViewState, search: string, back: () => void) => void;
}) {
  const state = useUrlBrowseState();
  const loc = useLocation();
  const nav = useNavigate();
  onReady(state, loc.search, () => nav(-1));
  return null;
}

function mount(initial: string) {
  let state!: BrowseViewState;
  let search = '';
  let back = () => {};
  render(
    <MemoryRouter initialEntries={[initial]}>
      <Harness
        onReady={(s, q, b) => {
          state = s;
          search = q;
          back = b;
        }}
      />
    </MemoryRouter>,
  );
  return {
    get state() { return state; },
    get search() { return search; },
    back: () => back(),
    get filters() { return fromSearchParams(new URLSearchParams(search)); },
  };
}

describe('loadPipelineView (URL adapter)', () => {
  it('replaces a narrowed cohort with the neutral pipeline view', () => {
    const h = mount('/browse?cat=byt&deal=pronajem&price_max=5000000&disp=2%2Bkk');
    act(() => h.state.loadPipelineView());
    expect(h.filters).toEqual(pipelineViewFilters());
    expect(h.filters.pipeline).toEqual({ stage_ids: [] });
    expect(h.filters.categoryMain).toEqual([]);
    expect(h.filters.categoryType).toBeNull();
    expect(h.filters.priceMax).toBeNull();
  });

  it('deselects the active preset in the same write', () => {
    const h = mount('/browse?preset=abc-123&cat=dum');
    expect(h.state.activePresetId).toBe('abc-123');
    act(() => h.state.loadPipelineView());
    expect(new URLSearchParams(h.search).has('preset')).toBe(false);
    expect(h.state.activePresetId).toBeNull();
  });

  it('keeps the map-overlay knobs (they are not cohort filters)', () => {
    const h = mount('/browse?cat=byt&rentmap=1&colorby=overall&tab=table');
    act(() => h.state.loadPipelineView());
    const sp = new URLSearchParams(h.search);
    expect(sp.get('rentmap')).toBe('1');
    expect(sp.get('colorby')).toBe('overall');
    expect(sp.get('tab')).toBe('table');
  });

  it('is undoable — the load pushes a history entry rather than replacing', () => {
    const h = mount('/browse?cat=dum&deal=prodej');
    act(() => h.state.loadPipelineView());
    expect(h.filters.pipeline).not.toBeNull();
    act(() => h.back());
    // Back is the undo for "the chip wiped my filters" — it only works because
    // the load PUSHES a history entry instead of replacing the current one.
    expect(h.filters.categoryMain).toEqual(['dum']);
    expect(h.filters.pipeline).toBeNull();
  });
});
