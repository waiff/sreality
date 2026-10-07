/* "Download all" in the property page's Photos header: the API zips the
 * listing's stored photos (R2 sends no CORS header, so the browser can't). */
import { useState } from 'react';
import { fetchListingPhotosZip } from '@/lib/api';
import type { ImagePublic } from '@/lib/types';

export default function DownloadPhotosButton({
  listingId,
  images,
  fileName,
}: {
  listingId: number;
  images: ImagePublic[];
  fileName: string;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Only photos with an R2 copy can be zipped; the rest live on the portal's CDN alone.
  const stored = images.filter((img) => img.storage_path != null).length;
  const partial = stored < images.length;

  const download = async () => {
    setBusy(true);
    setError(null);
    try {
      const blob = await fetchListingPhotosZip(listingId);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = fileName;
      document.body.appendChild(a);
      a.click();
      a.remove();
      // Firefox starts the save asynchronously; revoking at once can cancel it.
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Download failed');
    } finally {
      setBusy(false);
    }
  };

  const title =
    stored === 0
      ? 'No photo is stored yet — nothing to zip'
      : partial
        ? `${images.length - stored} of ${images.length} photos are not stored yet and are left out`
        : 'Download every photo as one .zip';

  return (
    <span className="inline-flex items-center gap-2">
      {error && (
        <span className="text-[0.7rem] text-[var(--color-brick)]" title={error} role="alert">
          Download failed
        </span>
      )}
      <button
        type="button"
        onClick={() => void download()}
        disabled={busy || stored === 0}
        title={title}
        className="inline-flex items-center gap-1.5 px-2.5 py-1 text-[0.75rem] rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[var(--color-ink-2)] enabled:hover:border-[var(--color-copper)] enabled:hover:text-[var(--color-copper)] disabled:opacity-60 disabled:cursor-not-allowed transition-colors"
      >
        <DownloadGlyph />
        <span>
          {busy
            ? 'Preparing zip…'
            : partial
              ? `Download ${stored} of ${images.length} (.zip)`
              : 'Download all (.zip)'}
        </span>
      </button>
    </span>
  );
}

function DownloadGlyph() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path
        d="M8 2v8M4.5 6.5 8 10l3.5-3.5M2.5 13.5h11"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
