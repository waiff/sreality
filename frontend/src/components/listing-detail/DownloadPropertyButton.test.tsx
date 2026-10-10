/* The property header's "Stáhnout": it asks the API for THIS property's zip (a PDF of
   the ad plus its stored photos), hands the browser a file, owns up when some photos
   have no stored copy, and says so when the download fails. */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import DownloadPropertyButton from './DownloadPropertyButton';
import * as api from '@/lib/api';
import type { ImagePublic } from '@/lib/types';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return { ...actual, fetchPropertyDownload: vi.fn() };
});

const fetchZip = vi.mocked(api.fetchPropertyDownload);

const photo = (id: number, storage_path: string | null): ImagePublic => ({
  id,
  sreality_id: 1,
  sequence: id,
  sreality_url: `https://cdn.example/${id}.jpg`,
  storage_path,
  clip_fine_tag: null,
  clip_logical_tag: null,
  clip_confidence: null,
  clip_render_score: null,
  phash: null,
});

describe('DownloadPropertyButton', () => {
  let clicked: string[];

  beforeEach(() => {
    fetchZip.mockReset();
    clicked = [];
    URL.createObjectURL = vi.fn(() => 'blob:zip');
    URL.revokeObjectURL = vi.fn();
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clicked.push(`${this.download}|${this.href}`);
    });
  });

  afterEach(() => vi.restoreAllMocks());

  it("downloads the property's zip", async () => {
    fetchZip.mockResolvedValue(new Blob(['zip']));
    render(
      <DownloadPropertyButton
        propertyId={7}
        images={[photo(1, '42/0001.jpg'), photo(2, '42/0002.jpg')]}
      />,
    );
    const button = screen.getByRole('button', { name: /stáhnout/i });
    expect(button.getAttribute('title')).toBe('PDF inzerátu a jeho fotky v jednom .zip');
    await userEvent.click(button);
    await waitFor(() => expect(clicked).toEqual(['property-7.zip|blob:zip']));
    expect(fetchZip).toHaveBeenCalledWith(7);
  });

  it('says which photos the zip leaves out when some are not stored yet', () => {
    render(
      <DownloadPropertyButton
        propertyId={7}
        images={[photo(1, '42/0001.jpg'), photo(2, null), photo(3, '42/0003.jpg')]}
      />,
    );
    expect(screen.getByRole('button').getAttribute('title')).toMatch(
      /2 z 3 fotek .* ostatní fotky zatím nejsou uložené/,
    );
  });

  it('stays enabled without a stored photo: the PDF alone is worth the download', () => {
    render(<DownloadPropertyButton propertyId={7} images={[photo(1, null)]} />);
    expect(screen.getByRole('button')).toBeEnabled();
  });

  it('reports a failed download instead of failing silently', async () => {
    fetchZip.mockRejectedValue(new Error('A photo could not be read from storage'));
    render(<DownloadPropertyButton propertyId={7} images={[photo(1, '42/0001.jpg')]} />);
    await userEvent.click(screen.getByRole('button'));
    const alert = await screen.findByRole('alert');
    expect(alert.getAttribute('title')).toBe('A photo could not be read from storage');
    expect(clicked).toEqual([]);
    expect(screen.getByRole('button')).toBeEnabled();
  });
});
