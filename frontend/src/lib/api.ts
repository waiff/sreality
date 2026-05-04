/* Minimal client for the FastAPI service. Browser → API for anything that
 * isn't a pure read off the public Supabase views (currently: Mapy.cz
 * proxy). The bearer token, when set, is shared across all callers — no
 * per-user identity. Token is never logged. */

const baseUrl = (import.meta.env.VITE_API_URL ?? '').replace(/\/$/, '');
const token = import.meta.env.VITE_API_TOKEN ?? '';

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown, message: string) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

const headers = (): HeadersInit => {
  const h: Record<string, string> = { 'content-type': 'application/json' };
  if (token) h['authorization'] = `Bearer ${token}`;
  return h;
};

const handle = async (res: Response): Promise<unknown> => {
  if (res.ok) return res.json();
  let detail: unknown = null;
  try {
    detail = await res.json();
  } catch {
    /* non-JSON body */
  }
  const message =
    detail && typeof detail === 'object' && 'detail' in detail
      ? String((detail as { detail: unknown }).detail)
      : `HTTP ${res.status}`;
  throw new ApiError(res.status, detail, message);
};

export const isApiConfigured = (): boolean => Boolean(baseUrl);

export const apiGet = async <T>(
  path: string,
  params?: Record<string, string | number | undefined>,
  signal?: AbortSignal,
): Promise<T> => {
  if (!baseUrl) throw new ApiError(0, null, 'API not configured');
  const url = new URL(baseUrl + path);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v != null) url.searchParams.set(k, String(v));
    }
  }
  const res = await fetch(url, { headers: headers(), signal });
  return handle(res) as Promise<T>;
};

export const apiPost = async <T>(
  path: string,
  body: unknown,
  signal?: AbortSignal,
): Promise<T> => {
  if (!baseUrl) throw new ApiError(0, null, 'API not configured');
  const res = await fetch(baseUrl + path, {
    method: 'POST',
    headers: headers(),
    body: JSON.stringify(body),
    signal,
  });
  return handle(res) as Promise<T>;
};
