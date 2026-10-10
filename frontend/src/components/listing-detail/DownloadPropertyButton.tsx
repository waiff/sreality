/* "Stáhnout" in the property page's header: one zip from the API holding a PDF of
 * the ad and the canonical ad's stored photos. The API builds it because R2 sends no
 * CORS header, so the browser can't read the photo bytes itself. */
import { useState } from 'react';
import { fetchPropertyDownload } from '@/lib/api';
import type { ImagePublic } from '@/lib/types';

export default function DownloadPropertyButton({
  propertyId,
  images,
}: {
  propertyId: number;
  /* The canonical ad's photos (the gallery's) — only to say which are left out. */
  images: ImagePublic[];
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Only photos with an R2 copy can be zipped; the rest live on the portal's CDN alone.
  const stored = images.filter((img) => img.storage_path != null).length;

  const download = async () => {
    setBusy(true);
    setError(null);
    try {
      const blob = await fetchPropertyDownload(propertyId);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `property-${propertyId}.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      // Firefox starts the save asynchronously; revoking at once can cancel it.
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Stažení selhalo');
    } finally {
      setBusy(false);
    }
  };

  const title =
    stored < images.length
      ? `PDF inzerátu a ${stored} z ${images.length} fotek v jednom .zip — ostatní fotky zatím nejsou uložené`
      : 'PDF inzerátu a jeho fotky v jednom .zip';

  return (
    <span className="inline-flex items-center gap-2">
      {error && (
        <span className="text-[0.75rem] text-[var(--color-brick)]" title={error} role="alert">
          Stažení selhalo
        </span>
      )}
      <button
        type="button"
        onClick={() => void download()}
        disabled={busy}
        title={title}
        className="inline-flex items-center gap-1.5 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-3 py-1.5 text-[0.8rem] text-[var(--color-ink-2)] transition-colors hover:border-[var(--color-copper)] hover:text-[var(--color-copper)] disabled:opacity-60 disabled:cursor-wait"
      >
        <DownloadGlyph />
        <span>{busy ? 'Připravuji…' : 'Stáhnout'}</span>
      </button>
    </span>
  );
}

function DownloadGlyph() {
  return (
    <svg className="h-4 w-4" viewBox="0 0 16 16" fill="none" aria-hidden>
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
