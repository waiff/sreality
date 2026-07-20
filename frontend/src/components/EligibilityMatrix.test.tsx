import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import EligibilityMatrix from './EligibilityMatrix';
import type { EligibilityBucket, EligibilityMatrix as MatrixPayload } from '@/lib/api';

const getEligibilityMatrix = vi.hoisted(() => vi.fn());
vi.mock('@/lib/api', () => ({ getEligibilityMatrix }));

function bucket(p: Partial<EligibilityBucket> & { n: number }): EligibilityBucket {
  const b: EligibilityBucket = {
    source: 'idnes',
    category_main: 'dum',
    is_active: true,
    has_street: true,
    has_disposition: true,
    has_geom: true,
    has_obec: true,
    has_area: true,
    elig_street: null,
    elig_geo: null,
    elig_byt_geo: null,
    ...p,
  };
  const cat = b.category_main;
  const geoFam = cat !== null && ['dum', 'pozemek', 'komercni', 'ostatni'].includes(cat);
  b.elig_street = b.has_street && b.has_disposition;
  b.elig_geo =
    cat === null ? null : b.is_active && geoFam && b.has_geom && b.has_obec && b.has_area;
  b.elig_byt_geo =
    cat === null
      ? null
      : b.is_active && cat === 'byt' && b.has_geom && b.has_obec && b.has_area && b.has_disposition;
  return b;
}

/* Shaped like the real production finding: idnes houses that carry a coordinate but no
 * Czech obec_id (foreign listings fall outside admin_boundaries), against a clean
 * sreality column. `has_disposition: false` throughout because that is the truth for
 * houses — which is exactly why the geo pass, not the street pass, is their only route. */
const PAYLOAD: MatrixPayload = {
  buckets: [
    bucket({ n: 19159, source: 'idnes', has_disposition: false }),
    bucket({ n: 7881, source: 'idnes', has_disposition: false, has_obec: false }),
    bucket({
      n: 871,
      source: 'idnes',
      has_disposition: false,
      has_obec: false,
      has_area: false,
    }),
    bucket({ n: 22178, source: 'sreality', has_disposition: false }),
    bucket({ n: 8, source: 'sreality', has_disposition: false, has_obec: false }),
  ],
  paths: [
    { key: 'street', domain_categories: null, active_only: false },
    { key: 'geo', domain_categories: ['dum', 'pozemek', 'komercni', 'ostatni'], active_only: true },
    { key: 'byt_geo', domain_categories: ['byt'], active_only: true },
  ],
  total: 50097,
};

/* The portal column comes before the totals column, so [0] is the portal's own cell.
 * Both can legitimately show the same number when one portal owns the whole figure. */
const firstButton = async (name: RegExp) =>
  (await screen.findAllByRole('button', { name }))[0];

function renderMatrix(onPick = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <EligibilityMatrix onPick={onPick} />
    </QueryClientProvider>,
  );
  return onPick;
}

describe('<EligibilityMatrix>', () => {
  beforeEach(() => {
    getEligibilityMatrix.mockReset();
    getEligibilityMatrix.mockResolvedValue(PAYLOAD);
  });

  it('renders the loss breakdown for a pass, with Czech-formatted counts', async () => {
    renderMatrix();
    await userEvent.click(await screen.findByRole('button', { name: /Geo \+ plocha/ }));

    // Scope, ineligible, and the dominant reason — the numbers the operator scans.
    await waitFor(() => expect(screen.getAllByText(/27\s?911/).length).toBeGreaterThan(0));
    expect(screen.getAllByText(/8\s?752/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/7\s?881/).length).toBeGreaterThan(0);
  });

  it('hands the parent a filter that reproduces the clicked reason bucket', async () => {
    const onPick = renderMatrix();
    await userEvent.click(await screen.findByRole('button', { name: /Geo \+ plocha/ }));

    await userEvent.click(await firstButton(/7\s?881/));

    expect(onPick).toHaveBeenCalledTimes(1);
    const f = onPick.mock.calls[0][0];
    expect(f).toMatchObject({
      source: 'idnes',
      category_main: 'dum',
      active: 'active',
      path: 'geo',
      path_state: 'ineligible',
    });
    // "obec_id absent, the pass's other inputs present" — exactly that bucket.
    expect(f.missing).toEqual(['obec_id']);
    expect([...f.has].sort()).toEqual(['area', 'geom']);
  });

  it('defaults to the summary tab and filters on whole-engine reachability there', async () => {
    const onPick = renderMatrix();
    // Houses carry no disposition, so the street pass cannot save them: the 871 rows
    // missing both obec and area are reachable by NO pass at all.
    await userEvent.click(await firstButton(/^871/));

    const f = onPick.mock.calls[0][0];
    expect(f.dedup).toBe('unreachable');
    expect(f.path).toBeUndefined();
  });

  it('shows the street pass losing every house to the missing disposition', async () => {
    // The structural finding the matrix exists to make visible: the street pass has no
    // category gate, but a house never has a disposition, so it reaches none of them.
    const onPick = renderMatrix();
    await userEvent.click(await screen.findByRole('button', { name: /Ulice \+ dispozice/ }));
    await userEvent.click(await firstButton(/^27\s?911/)); // idnes scope == idnes loss

    const f = onPick.mock.calls[0][0];
    expect(f.path).toBe('street');
    expect(f.category_main).toBe('dum');
  });

  it('shows a dash where a pass has no listings in scope', async () => {
    getEligibilityMatrix.mockResolvedValue({ ...PAYLOAD, buckets: [] });
    renderMatrix();
    expect(await screen.findByText(/Žádná data pro tento rozsah/)).toBeInTheDocument();
  });
});
