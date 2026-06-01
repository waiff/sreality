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

// Cache-bust token appended to every API image URL. The /images route keys only on
// the path, so the query is ignored server-side — but changing this value makes the
// browser/edge treat it as a fresh URL, flushing any redirect cached against the old
// URL. BUMP THIS after a serve-path change (e.g. an R2 credential rotation) to clear
// cover images that browsers cached while the path was broken.
const IMG_CACHE_BUST = '2';

// sreality's CDN 401s a BARE image URL — it only serves bytes with the render-transform
// query present (mirrors scraper/image_storage.py `_with_transform`). Many stored
// `sreality_url`s are bare, so the fallback below must append it or every not-yet-in-R2
// sreality photo 401s. Gated on the sdn.cz host + absence of an existing `fl=`.
const SREALITY_IMG_HOST = 'sdn.cz';
const SREALITY_TRANSFORM = 'fl=res,749,562,3|shr,,20|jpg,90';

const withSrealityTransform = (url: string): string => {
  if (!url.includes(SREALITY_IMG_HOST) || url.includes('fl=')) return url;
  return `${url}${url.includes('?') ? '&' : '?'}${SREALITY_TRANSFORM}`;
};

export interface ImageRef {
  sreality_url: string;
  storage_path: string | null;
}

export const imageSrc = (img: ImageRef): string => {
  if (API_BASE && img.storage_path) {
    return `${API_BASE}/images/${img.storage_path}?v=${IMG_CACHE_BUST}`;
  }
  // No R2 copy yet → fall back to the original CDN. sreality needs the render-transform
  // appended (a bare sdn.cz URL 401s); other portals serve their bare URLs directly.
  return withSrealityTransform(img.sreality_url);
};
