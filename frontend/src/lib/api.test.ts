/* Query-param serialization + the auth mode in lib/api's request(). Imported
 * dynamically because BASE_URL and TOKEN are read from import.meta.env at
 * module-evaluation time. Every /brokers call passes `jwt: true` — those routes
 * are verify_jwt-gated and 401 on the static token. */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { supabase } from './supabase';

async function loadApi() {
  vi.stubEnv('VITE_API_BASE_URL', 'https://api.test.invalid');
  vi.stubEnv('VITE_API_TOKEN', 'STATIC-BUNDLE-TOKEN');
  return import('./api');
}

function captureFetch(): { urls: string[]; headers: Record<string, string>[] } {
  const urls: string[] = [];
  const headers: Record<string, string>[] = [];
  vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    urls.push(String(input));
    headers.push((init?.headers ?? {}) as Record<string, string>);
    return { ok: true, status: 200, statusText: 'OK', text: async () => '{}' } as Response;
  });
  return { urls, headers };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe('apiGet query params', () => {
  it('serializes an array as repeated params, not a comma join', async () => {
    const { urls } = captureFetch();
    const { apiGet } = await loadApi();
    await apiGet('/brokers', { ids: [1, 2, 3] }, undefined, true);
    expect(new URL(urls[0]).search).toBe('?ids=1&ids=2&ids=3');
  });

  it('omits an empty array and nullish values entirely', async () => {
    const { urls } = captureFetch();
    const { apiGet } = await loadApi();
    await apiGet('/brokers', { ids: [], q: undefined, limit: 5 }, undefined, true);
    expect(new URL(urls[0]).search).toBe('?limit=5');
  });

  it('drops an empty-string value instead of emitting a meaningless param', async () => {
    /* THE REGRESSION. `[null].join(',')` is '' in JS, which used to be sent as
     * `?listing_ids=`. The API read the empty value as "no filter" and answered
     * with the entire estimation_runs table. An empty string is not a filter. */
    const { urls } = captureFetch();
    const { apiGet } = await loadApi();
    await apiGet('/estimations', { listing_ids: '', limit: 5 }, undefined, true);
    expect(new URL(urls[0]).search).toBe('?limit=5');
  });

  it('still sets a scalar once', async () => {
    const { urls } = captureFetch();
    const { apiGet } = await loadApi();
    await apiGet('/brokers/search', { q: 'alfa', limit: 12 }, undefined, true);
    expect(new URL(urls[0]).search).toBe('?q=alfa&limit=12');
  });
});

describe('apiGet auth mode', () => {
  it('sends the caller session JWT when jwt: true — what /brokers/* requires', async () => {
    vi.spyOn(supabase.auth, 'getSession').mockResolvedValue({
      data: { session: { access_token: 'USER-JWT' } },
      error: null,
    } as unknown as Awaited<ReturnType<typeof supabase.auth.getSession>>);
    const { headers } = captureFetch();
    const { apiGet } = await loadApi();
    await apiGet('/brokers/leaderboard', { limit: 5 }, undefined, true);
    expect(headers[0].Authorization).toBe('Bearer USER-JWT');
  });

  it('falls back to the static bundle token when jwt is omitted — a 401 on /brokers/*', async () => {
    const { headers } = captureFetch();
    const { apiGet } = await loadApi();
    await apiGet('/brokers/leaderboard', { limit: 5 });
    expect(headers[0].Authorization).toBe('Bearer STATIC-BUNDLE-TOKEN');
  });
});

describe('estimation subject identity', () => {
  it('fetches property-grain runs by SURROGATE listing ids, not sreality ids', async () => {
    /* listings.sreality_id is NULL for every non-sreality listing (migration
     * 311's sign check), so keying this fetch on it silently dropped those
     * subjects — and an all-null id array collapsed to an unfiltered request. */
    const { urls } = captureFetch();
    const { fetchEstimationsForListings } = await import('./queries');
    await fetchEstimationsForListings([501, 502]);
    const search = new URL(urls[0]).search;
    expect(search).toContain('listing_ids=501%2C502');
    expect(search).not.toContain('sreality_ids');
  });

  it('sends the caller JWT so account scoping resolves the operator, not SYSTEM', async () => {
    vi.spyOn(supabase.auth, 'getSession').mockResolvedValue({
      data: { session: { access_token: 'USER-JWT' } },
      error: null,
    } as unknown as Awaited<ReturnType<typeof supabase.auth.getSession>>);
    const { headers } = captureFetch();
    const { fetchEstimationsForListings } = await import('./queries');
    await fetchEstimationsForListings([501]);
    expect(headers[0].Authorization).toBe('Bearer USER-JWT');
  });
});

describe('postAutodedupVerdict', () => {
  /* THE STORED ROW IS NESTED. `POST /autodedup/verdict` answers
   * `{"data": {"verdict": {…row…}, "must_not_link": bool}}`; taking `data`
   * verbatim hands the badge an object where it expects a verdict string, so a
   * SUCCESSFUL write un-presses the button it just set — on the one write
   * surface of the whole program. Pinned against the route's literal shape. */
  function replyWith(body: unknown): void {
    vi.stubGlobal('fetch', async () => ({
      ok: true,
      status: 200,
      statusText: 'OK',
      text: async () => JSON.stringify(body),
    }) as Response);
  }

  it('unwraps the row out of {verdict, must_not_link}', async () => {
    replyWith({
      data: {
        verdict: {
          id: 4,
          kind: 'pair',
          listing_lo: 101,
          listing_hi: 202,
          cluster_key: null,
          verdict: 'different',
          note: null,
          decided_by: 'operator@example.invalid',
          decided_at: '2026-09-16T10:00:00Z',
        },
        must_not_link: true,
      },
      store_ready: true,
    });
    const { postAutodedupVerdict } = await loadApi();
    const res = await postAutodedupVerdict({
      kind: 'pair',
      listing_lo: 101,
      listing_hi: 202,
      verdict: 'different',
    });
    expect(res.data?.verdict).toBe('different');
    expect(res.data?.decided_by).toBe('operator@example.invalid');
    expect(res.must_not_link).toBe(true);
    expect(res.store_ready).toBe(true);
  });

  it('is a null row, not an empty object, when the store is not ready', async () => {
    replyWith({ data: null, store_ready: false });
    const { postAutodedupVerdict } = await loadApi();
    const res = await postAutodedupVerdict({ kind: 'cluster', cluster_key: 7, verdict: 'same' });
    expect(res.data).toBeNull();
    expect(res.must_not_link).toBe(false);
  });
});

describe('request() error detail', () => {
  /* FastAPI answers a 422 with `detail` as a LIST of {loc, msg, type}. The
   * banner used to read "[object Object]" — the operator could not tell WHICH
   * parameter was rejected. */
  it('renders a 422 validation list as readable text', async () => {
    vi.stubGlobal('fetch', async () => ({
      ok: false,
      status: 422,
      statusText: 'Unprocessable Entity',
      text: async () =>
        JSON.stringify({
          detail: [
            { loc: ['query', 'min_size'], msg: 'Input should be a valid integer', type: 'int_parsing' },
          ],
        }),
    }) as Response);
    const { apiGet } = await loadApi();
    await expect(apiGet('/autodedup/groups', {}, undefined, true)).rejects.toThrow(
      /min_size: Input should be a valid integer/,
    );
  });
});

/* ------------------------------------------------------------------ wire conformance
 *
 * The /autodedup types describe a server this file cannot call, so the only thing
 * standing between them and drift is a literal payload. These are VERBATIM bodies
 * captured from `api/routes/autodedup.py` driven through FastAPI's TestClient with
 * the route tests' own fake connection — not hand-written approximations.
 *
 * The load-bearing part is the TYPE ANNOTATION, not the assertion: `npx tsc --noEmit`
 * (a CI step) rejects a key the interface does not declare and a null where it
 * promises a value, so a server that starts spelling a field differently fails the
 * build instead of rendering `undefined` on an admin page. The `expect`s exist so
 * vitest owns the block too.
 *
 * WHAT THIS ALREADY CAUGHT: `AutodedupEngineStats` described a shape the server has
 * never sent (`zones`/`n_clusters`/`n_certificates`/`verdicts: Record<>` against the
 * real `pairs_by_zone`/`certificates`/`generations[]`/`verdicts[]`), and neither
 * `total_floors` (residual sides) nor `listing_id` (image rows) nor the pair row's
 * `feature_version`/`model_version`/`decided_at` were declared at all. All of it
 * compiled, because nothing ever assigned a real body to the type.
 */
describe('autodedup wire conformance', () => {
  it('types the /stats engine block as the route actually sends it', async () => {
    const { getAutodedupStats } = await loadApi();
    const engine: import('./api').AutodedupEngineStats = {
      pairs_by_zone: { merge: 4, band: 9 },
      n_pairs: 13,
      certificates: { 'K-A': 3 },
      generations: [
        {
          generation: 'g1',
          n_clusters: 13,
          n_members: 26,
          n_conflicted: 2,
          last_changed_at: '2026-09-16T00:00:00+00:00',
        },
      ],
      latest_generation: 'g1',
      verdicts: [{ kind: 'pair', verdict: 'different', n: 2 }],
      n_verdicts: 2,
      judgements: [{ tier: 'text', verdict: 'same_property', n: 5 }],
      n_judgements: 5,
      last_score_run: {
        id: 7,
        status: 'success',
        fingerprint: 'abc123',
        cohort: { blocks: 12 },
        params: { generation: 'g1' },
        stats: { n_pairs: 13 },
        started_at: '2026-09-16T00:00:00+00:00',
        finished_at: '2026-09-16T01:00:00+00:00',
      },
    };
    const stats: import('./api').AutodedupStats = {
      n_iterations: 1,
      total_cost_usd: 1.0,
      run_cap_usd: 25.0,
      program_cap_usd: 200.0,
      last_iteration_at: '2026-09-16T00:00:00+00:00',
      waves: [{ wave: 'W5', n: 1, last_status: 'done', cost_usd: 1.0 }],
      engine,
    };
    vi.stubGlobal('fetch', async () => ({
      ok: true,
      status: 200,
      statusText: 'OK',
      text: async () => JSON.stringify({ data: stats, store_ready: true }),
    }));
    const res = await getAutodedupStats();
    /* `cohort` is null for the whole time a score run is `running` — the lane
     * writes it on the terminal UPDATE, once the dataset has loaded. */
    expect(res.data?.engine?.last_score_run?.cohort).toEqual({ blocks: 12 });
    expect(res.data?.engine?.latest_generation).toBe('g1');
  });

  it('types a residual side and a cluster member as the routes send them', async () => {
    /* One member of a proposed group (no `total_floors` in that select list) and
     * one side of a residual pair (which has it) are the SAME type — so the
     * storey count is optional rather than promised and absent. */
    const groupMember: import('./api').AutodedupMember = {
      listing_id: 11,
      source: 'sreality',
      source_url: 'https://www.sreality.cz/detail/11',
      category_main: 'byt',
      category_type: 'prodej',
      disposition: '3+kk',
      area_m2: 68.0,
      floor: 3,
      price_czk: 8_900_000,
      first_seen_at: '2026-01-02T00:00:00+00:00',
      last_seen_at: '2026-09-01T00:00:00+00:00',
      is_active: true,
      n_images: 12,
      cover: {
        storage_path: 'listings/11/1.jpg',
        sreality_url: 'https://img.example.invalid/11/1.jpg',
      },
    };
    const residualSide: import('./api').AutodedupMember = {
      ...groupMember,
      listing_id: 21,
      total_floors: 5,
    };
    expect(groupMember.total_floors).toBeUndefined();
    expect(residualSide.total_floors).toBe(5);
  });

  it('types a stored pair row with the provenance the detail routes send', async () => {
    const pair: import('./api').AutodedupPairRow = {
      listing_lo: 11,
      listing_hi: 12,
      probes: ['K1', 'K5'],
      families: 33,
      features: { area_rel_diff: [0.004, true], dispo_equal: [1.0, true] },
      score: 0.71,
      zone: 'band',
      decision: 'certificate:K-A:evidence_gate',
      guard_veto: null,
      cluster_key: 101,
      feature_version: 1,
      model_version: 'hand_v1',
      decided_at: '2026-09-16T05:00:00+00:00',
      certificate: 'K-A',
      family_names: ['ATTR', 'IMG'],
      why_not_merged: 'in the review band',
    };
    const { decodeFamilies } = await loadApi();
    /* The chips read the decoded names when the server sends them and fall back
     * to the mask otherwise — the same 33 either way. */
    expect(decodeFamilies(pair.family_names)).toEqual(['ATTR', 'IMG']);
    expect(decodeFamilies(pair.families)).toEqual(['ATTR', 'IMG']);
  });

  it('types an image row with the listing it belongs to', async () => {
    const image: import('./api').AutodedupPairImage = {
      listing_id: 11,
      image_id: 900,
      sequence: 1,
      storage_path: 'listings/11/1.jpg',
      sreality_url: 'https://img.example.invalid/11/1.jpg',
      phash: '1234567890',
      best_hamming: 0,
      best_match_image_id: 911,
    };
    expect(image.listing_id).toBe(11);
  });
});
