/* The Photos header's "download all": it asks the API for THIS listing's zip,
   hands the browser a file under the given name, owns up when some photos have
   no stored copy, and says so when the download fails. */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import DownloadPhotosButton from './DownloadPhotosButton';
import * as api from '@/lib/api';
import type { ImagePublic } from '@/lib/types';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return { ...actual, fetchListingPhotosZip: vi.fn() };
});

const fetchZip = vi.mocked(api.fetchListingPhotosZip);

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

describe('DownloadPhotosButton', () => {
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

  it('downloads the listing zip under the given file name', async () => {
    fetchZip.mockResolvedValue(new Blob(['zip']));
    render(
      <DownloadPhotosButton
        listingId={42}
        images={[photo(1, '42/0001.jpg'), photo(2, '42/0002.jpg')]}
        fileName="property-7-photos.zip"
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: /download all/i }));
    await waitFor(() => expect(clicked).toEqual(['property-7-photos.zip|blob:zip']));
    expect(fetchZip).toHaveBeenCalledWith(42);
  });

  it('says how many photos the zip will hold when some are not stored yet', () => {
    render(
      <DownloadPhotosButton
        listingId={42}
        images={[photo(1, '42/0001.jpg'), photo(2, null), photo(3, '42/0003.jpg')]}
        fileName="x.zip"
      />,
    );
    const button = screen.getByRole('button', { name: /download 2 of 3/i });
    expect(button.getAttribute('title')).toMatch(/1 of 3 photos are not stored/);
  });

  it('is disabled when no photo is stored', () => {
    render(<DownloadPhotosButton listingId={42} images={[photo(1, null)]} fileName="x.zip" />);
    expect(screen.getByRole('button')).toBeDisabled();
  });

  it('reports a failed download instead of failing silently', async () => {
    fetchZip.mockRejectedValue(new Error('A photo could not be read from storage'));
    render(
      <DownloadPhotosButton listingId={42} images={[photo(1, '42/0001.jpg')]} fileName="x.zip" />,
    );
    await userEvent.click(screen.getByRole('button'));
    const alert = await screen.findByRole('alert');
    expect(alert.getAttribute('title')).toBe('A photo could not be read from storage');
    expect(clicked).toEqual([]);
    expect(screen.getByRole('button')).toBeEnabled();
  });
});
