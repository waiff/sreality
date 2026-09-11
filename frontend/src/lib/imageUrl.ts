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
// cover images that browsers cached while the path was broken. It CANNOT flush image
// BYTES: those are cached under the presigned R2 URL the redirect mints, which is
// day-anchored and therefore rolls over on its own at 00:00 UTC.
const IMG_CACHE_BUST = '2';

// sreality's CDN 401s a BARE image URL and is an exact-template ALLOWLIST — only their
// SQUARE_1800_JPG chain (whole frame, up to 1800px, no watermark) returns the master.
// Mirrors scraper/image_storage.py `with_transform` (parity-tested): drop the serving
// ops a stored chain carries (including the legacy 749 CROP), keep per-photo ops such as
// `rot,<deg>,0` in front, append ours. Gated on the sdn.cz host.
const SREALITY_IMG_HOST = 'sdn.cz';
const SREALITY_TRANSFORM_OPS = 'res,1800,1800,1|shr,,20|jpg,80';
const SERVING_OP_HEADS = new Set(['res', 'shr', 'jpg', 'webp', 'wrm']);

const withSrealityTransform = (url: string): string => {
  if (!url.includes(SREALITY_IMG_HOST)) return url;
  const [head, ...fragmentParts] = url.split('#');
  const fragment = fragmentParts.length ? `#${fragmentParts.join('#')}` : '';
  const queryAt = head.indexOf('?');
  if (queryAt < 0) return `${head}?fl=${SREALITY_TRANSFORM_OPS}${fragment}`;
  const base = head.slice(0, queryAt);
  const others: string[] = [];
  const preserved: string[] = [];
  for (const param of head.slice(queryAt + 1).split('&')) {
    if (!param.startsWith('fl=')) {
      if (param) others.push(param);
      continue;
    }
    for (const op of param.slice(3).split('|')) {
      if (op && !SERVING_OP_HEADS.has(op.split(',')[0])) preserved.push(op);
    }
  }
  const chain = [...preserved, SREALITY_TRANSFORM_OPS].join('|');
  return `${base}?${[...others, `fl=${chain}`].join('&')}${fragment}`;
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
  // normalised onto the URL (a bare sdn.cz URL 401s, and a stored legacy chain would
  // serve the 4:3 crop); other portals serve their bare URLs directly.
  return withSrealityTransform(img.sreality_url);
};
