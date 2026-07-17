/* MergeDecisionsChip — extracted out of ListingDetail so PropertyDetail can
 * share the exact same query + link (see the component's own doc comment).
 * Pins: renders nothing for a singleton / zero-decision property, and renders
 * the count + link once the audit read resolves. A 403 for a non-admin
 * session (the API route is admin-gated) degrades the same as zero decisions
 * — no data, no crash.
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import { MergeDecisionsChip } from './MergeDecisionsChip';
import * as api from '@/lib/api';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return { ...actual, getDedupAudit: vi.fn() };
});

const auditMock = vi.mocked(api.getDedupAudit);

function renderChip(propertyId: number | null, multiSource: boolean) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <MergeDecisionsChip propertyId={propertyId} multiSource={multiSource} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('<MergeDecisionsChip>', () => {
  it('renders nothing for a singleton property (query disabled)', () => {
    const { container } = renderChip(3309, false);
    expect(auditMock).not.toHaveBeenCalled();
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when the property has zero recorded merges', async () => {
    auditMock.mockResolvedValue({ data: [], total: 0, returned: 0 });
    const { container } = renderChip(3309, true);
    await waitFor(() => expect(auditMock).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it('links to the dedup audit history scoped to this property', async () => {
    auditMock.mockResolvedValue({ data: [], total: 3, returned: 0 });
    renderChip(3309, true);
    const link = await screen.findByRole('link');
    expect(link).toHaveAttribute('href', '/dedup?audit_property=3309#history');
    expect(link).toHaveTextContent('3 rozhodnutí');
  });
});
