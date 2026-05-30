/**
 * Single source of truth for turning a listing image into a loadable URL.
 *
 * Listing photos live in Cloudflare R2 (durable) but the sreality CDN URLs we
 * scraped expire within weeks. We serve the R2 copy through the API's
 * `GET /images/{storage_path}` redirect (a presigned URL), so a private bucket
 * still reaches the browser and no R2 base needs baking into the build.
 *
 * Fallback to `sreality_url` only when there's no R2 copy yet (a just-scraped
 * listing whose bytes the async image job hasn't downloaded) or no API base
 * (local dev) — its CDN URL may still be live for a day or two. Once R2 has the
 * bytes (`storage_path` set) we always prefer the durable path. Callers render
 * a placeholder on the `<img onError>` for the dead-CDN case.
 */

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '');

export interface ImageRef {
  sreality_url: string;
  storage_path: string | null;
}

export const imageSrc = (img: ImageRef): string => {
  if (API_BASE && img.storage_path) {
    return `${API_BASE}/images/${img.storage_path}`;
  }
  return img.sreality_url;
};
