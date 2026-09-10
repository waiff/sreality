/* LocationCompare — the review bench renders its scope, its sections, and the
 * okres table the scope response feeds.
 *
 * Hermetic: every `/location/compare/*` wrapper is mocked, and `lib/basemap`'s
 * createMap is stubbed so the two MapLibre panes mount without WebGL (jsdom has
 * none). The stub is deliberately inert — nothing here asserts on map paint;
 * what is pinned is that the page's own data regions render, and that the map
 * section fetches for the viewport the map opens on. */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import LocationCompare from './LocationCompare';
import * as lc from '../lib/locationCompare';
import type {
  CompareMap, CompareScope, CompareUnit, CompareUnits, OkresRow,
} from '../lib/locationCompare';

vi.mock('../lib/locationCompare', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/locationCompare')>();
  return {
    ...actual,
    fetchCompareScope: vi.fn(),
    fetchCompareUnits: vi.fn(),
    fetchCompareUnit: vi.fn(),
    fetchCompareStreets: vi.fn(),
    fetchCompareMap: vi.fn(),
    fetchCompareRadius: vi.fn(),
  };
});

/* The stub fires `load` the way MapLibre does — that is the only event the real
 * map emits for its own opening viewport, so a stub that swallows it cannot see
 * a page that never fetches until the operator pans. */
vi.mock('@/lib/basemap', () => ({
  TILE_STYLE: 'stub',
  createMap: () => ({
    on: (event: string, a: unknown, b?: unknown) => {
      const handler = typeof a === 'function' ? a : b;
      if (event === 'load' && typeof handler === 'function') {
        queueMicrotask(() => (handler as () => void)());
      }
    },
    off: () => {},
    remove: () => {},
    addControl: () => {},
    addSource: () => {},
    addLayer: () => {},
    getSource: () => undefined,
    getCanvas: () => ({ style: {} }),
    getBounds: () => ({ getWest: () => 14, getSouth: () => 50, getEast: () => 15, getNorth: () => 51 }),
    getCenter: () => ({ lat: 50, lng: 14 }),
    getZoom: () => 9,
    jumpTo: () => {},
    setFeatureState: () => {},
    project: () => ({ x: 0, y: 0 }),
  }),
}));

const okres = (okres_kod: number, name: string, kraj_kod: number): OkresRow => ({
  okres_kod, kraj_kod, name,
  n_old: 100, n_new_certain: 80, n_new_possible: 12, n_new_no: 5,
  n_no_row: 3, n_only_old: 8, n_only_new: 4, agreement_pct: 92,
});

const scope: CompareScope = {
  generated_at: '2026-09-10T08:00:00Z',
  kraje: [19, 27],
  kraje_rows: [{
    kraj_kod: 19, name: 'Hlavní město Praha',
    n_old: 100, n_new_certain: 80, n_new_possible: 12, n_new_no: 5,
    n_no_row: 3, n_only_old: 8, n_only_new: 4, agreement_pct: 92,
  }],
  okresy: [okres(3100, 'Praha', 19), okres(2109, 'Kladno', 27)],
  by_method: [{ admin_assignment_method: 'registry', n: 91 }],
  by_source: [{ source: 'sreality', n_old: 100, n_new_certain: 80, n_new_possible: 12, n_no_row: 3 }],
};

const units: CompareUnits = {
  generated_at: scope.generated_at,
  kraje: [19, 27],
  level: 'obec',
  parent_kod: 2109,
  rows: [{
    code: 532053, name: 'Kladno', n_old: 40, n_new_certain: 30,
    n_new_possible: 6, n_only_old: 4, n_only_new: 1,
  }],
};

const unit: CompareUnit = {
  generated_at: scope.generated_at,
  kraje: [19, 27],
  level: 'okres',
  code: 3100,
  name: 'Praha',
  counts: {
    n_old: 100, n_new_certain: 80, n_new_possible: 12, n_new_no: 5,
    n_no_row: 3, n_only_old: 8, n_only_new: 4, n_claimed: 2,
  },
  by_method: [{ admin_assignment_method: 'pip_containment', n: 9 }],
  by_source: [{ source: 'bazos', n_old: 10, n_new_certain: 8, n_new_possible: 1, n_no_row: 1 }],
  only_old: [{
    property_id: 7, listing_id: 70, source: 'bazos',
    old_label: 'Praha 5', new_label: null, granularity: 'obec',
    match_confidence: 'low', admin_assignment_method: null,
    uncertainty_radius_m: null, distance_to_nearest_boundary_m: null,
    verdict: 'no_row', reason: 'no_projection_row',
  }],
  only_new: [],
};

const emptyMap: CompareMap = {
  generated_at: scope.generated_at,
  kraje: [19, 27],
  rows: [],
  truncated: false,
  counts: {
    both: 0, only_old_geom: 0, only_new_geom: 0,
    moved_gt_100m: 0, demoted_to_circle: 0, no_geom_either: 0,
  },
};

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <LocationCompare />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(lc.fetchCompareScope).mockResolvedValue(scope);
  vi.mocked(lc.fetchCompareUnits).mockResolvedValue(units);
  vi.mocked(lc.fetchCompareUnit).mockResolvedValue(unit);
  vi.mocked(lc.fetchCompareMap).mockResolvedValue(emptyMap);
  vi.mocked(lc.fetchCompareStreets).mockResolvedValue({
    generated_at: scope.generated_at, rows: [],
  });
});

describe('LocationCompare', () => {
  it('renders both kraj scope chips, pressed by default', async () => {
    renderPage();
    expect(screen.getByRole('button', { name: 'Praha (19)' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Středočeský (27)' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('renders every section heading', async () => {
    renderPage();
    await screen.findByRole('heading', { name: 'Filters — okresy' });
    expect(screen.getByRole('heading', { name: 'Map — old vs new' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Radius' })).toBeInTheDocument();
  });

  it('turns the scope response into okres rows', async () => {
    renderPage();
    expect(await screen.findByText('Kladno')).toBeInTheDocument();
    expect(screen.getByText('Praha')).toBeInTheDocument();
    expect(screen.getAllByText('92.0%')).toHaveLength(scope.okresy.length + 1);
  });

  it('keeps at least one kraj on scope', async () => {
    renderPage();
    const praha = screen.getByRole('button', { name: 'Praha (19)' });
    fireEvent.click(praha);
    expect(praha).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(screen.getByRole('button', { name: 'Středočeský (27)' }));
    expect(screen.getByRole('button', { name: 'Středočeský (27)' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('fetches the map for the opening viewport, with no pan', async () => {
    renderPage();
    await waitFor(() => expect(lc.fetchCompareMap).toHaveBeenCalled(), { timeout: 3000 });
    expect(vi.mocked(lc.fetchCompareMap).mock.calls[0][0]).toEqual({
      west: 14, south: 50, east: 15, north: 51,
    });
  });

  it('counts "no new position" from the server, not from the capped page', async () => {
    vi.mocked(lc.fetchCompareMap).mockResolvedValue({
      ...emptyMap,
      truncated: true,
      counts: { ...emptyMap.counts, only_old_geom: 42 },
    });
    renderPage();
    expect(await screen.findByText(/42 with no new position/, {}, { timeout: 3000 }))
      .toBeInTheDocument();
  });

  it('opens the unit detail when an okres row is clicked', async () => {
    renderPage();
    fireEvent.click(await screen.findByText('Kladno'));
    expect(await screen.findByText("Old shows, new doesn't")).toBeInTheDocument();
    expect(screen.getByText('no projection row')).toBeInTheDocument();
  });
});
