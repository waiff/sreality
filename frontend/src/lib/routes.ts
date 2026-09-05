/* The ONE table of in-app route patterns.
 *
 * WHY THIS EXISTS. Route paths were stringly-typed and duplicated: `routes.tsx`
 * declared them, `Shell`'s nav table re-typed them, and ~46 `to=` literals plus
 * ~10 `navigate('/…')` calls re-typed them again. Nothing tied the copies
 * together, and the failure mode is silent — a renamed route falls through to
 * `routes.tsx`'s `path: '*'` catch-all and renders NotFound with an HTTP 200.
 * No type error, no test failure, no server log.
 *
 * THE PATTERN IS THE ONLY AUTHORED STRING. `def()` derives `build()` from the
 * pattern via react-router's own `generatePath`, so a builder cannot drift from
 * the pattern it builds — there is no second string to keep in sync. Renaming a
 * param (`:id` → `:brokerId`) is a compile error at every call site.
 *
 * `routes.tsx` CONSUMES this table (`childPath` / `pattern`), so the router and
 * this registry are not two tables that a test has to reconcile — they are one
 * table with two readers. That is why there is no "does routes.tsx agree with
 * ROUTES" test: the question cannot arise.
 *
 * WHAT THE `RoutePath` BRAND DOES AND DOES NOT BUY. It marks a string as
 * registry-derived and makes `withQuery`/`withHash` composition type-safe. It
 * does NOT make `<Link to="/brokers/7">` a compile error — react-router's own
 * `LinkProps.to` is `string | Partial<Path>`, and a plain string satisfies that
 * whatever we brand. Enforcement against hand-typed literals is a lint rail, not
 * a type; anchor-ness itself is only provable by a test that asserts the
 * rendered role. Do not mistake the brand for a gate.
 *
 * NOT AN API-PATH REGISTRY. `lib/api.ts` and `lib/brokers.ts` hold ~120
 * byte-identical endpoint strings (`/brokers/${id}` is both an SPA route and an
 * API path). Those are a different namespace and stay where they are. */
import { generatePath } from 'react-router-dom';

declare const routePathBrand: unique symbol;

/* A path built from this registry. Structurally a string, so it flows into
 * `<Link to>` / `navigate()` unchanged. */
export type RoutePath = string & { readonly [routePathBrand]: true };

/* The `:param` names inside a pattern, as a union — `never` when there are
 * none, which is what makes `build()` argument-less for a static route. */
type PathParam<P extends string> = P extends `${string}:${infer Rest}`
  ? Rest extends `${infer Name}/${infer Tail}`
    ? Name | PathParam<`/${Tail}`>
    : Rest
  : never;

type BuildArgs<P extends string> = [PathParam<P>] extends [never]
  ? []
  : [params: Record<PathParam<P>, string | number>];

type RouteDef<P extends string> = {
  /* Absolute pattern, e.g. `/brokers/:id`. The matching/census form. */
  readonly pattern: P;
  /* The same pattern relative to the `/` parent route, e.g. `brokers/:id`.
   * `routes.tsx` declares its children relative; this is the only consumer. */
  readonly childPath: string;
  readonly build: (...args: BuildArgs<P>) => RoutePath;
};

function def<P extends `/${string}`>(pattern: P): RouteDef<P> {
  return {
    pattern,
    childPath: pattern.slice(1),
    build: (...args: BuildArgs<P>) => {
      const params = (args[0] ?? {}) as Record<string, string | number>;
      // Two coercions, both load-bearing:
      //   String() — ids reach us as numbers far more often than as strings,
      //   so coerce here rather than at ~50 call sites.
      //   encodeURIComponent() — `generatePath` substitutes RAW (verified
      //   against react-router 6.30.3: a param of "a/b" yields "/listing/a/b",
      //   silently changing the route's shape). A portal's native id is
      //   free-form text, so an unencoded "/" or "?" would build a URL that
      //   resolves somewhere else entirely. Encoding here preserves what
      //   listingCanonicalPath did by hand and makes every future builder safe
      //   by construction.
      const safe: Record<string, string> = {};
      for (const [k, v] of Object.entries(params)) safe[k] = encodeURIComponent(String(v));
      return generatePath(pattern, safe as never) as RoutePath;
    },
  };
}

export const ROUTES = {
  // Full-page auth screens — declared as absolute top-level routes in
  // routes.tsx (outside the Shell), so they consume `pattern`, not `childPath`.
  login: def('/login'),
  forgotPassword: def('/forgot-password'),
  resetPassword: def('/reset-password'),

  browse: def('/browse'),
  // Bare /listing serves the ?property=<id> query form; the two parameterised
  // forms are the canonical natural key and the legacy numeric resolver. All
  // three are documented in lib/listingUrl.ts, which owns the precedence.
  listing: def('/listing'),
  listingCanonical: def('/listing/:source/:nativeId'),
  listingLegacy: def('/listing/:sreality_id'),
  health: def('/health'),
  costs: def('/costs'),
  estimations: def('/estimations'),
  estimationDetail: def('/estimation/:id'),
  brokers: def('/brokers'),
  // Declared before `/brokers/:id` in routes.tsx so the literal wins the match.
  brokersReview: def('/brokers/review'),
  brokerDetail: def('/brokers/:id'),
  outreach: def('/outreach'),
  outreachDetail: def('/outreach/:id'),
  buildingDetail: def('/building/:id'),
  collections: def('/collections'),
  collectionDetail: def('/collection/:id'),
  pipeline: def('/pipeline'),
  datasets: def('/datasets'),
  watchdog: def('/watchdog'),
  watchdogManage: def('/watchdog/manage'),
  watchdogEdit: def('/watchdog/:id/edit'),
  notifications: def('/notifications'),
  locationQuality: def('/location-quality'),
  settings: def('/settings'),
  newDedup: def('/new-dedup'),
  newDedupSettings: def('/new-dedup/settings'),
  newDedupLabeling: def('/new-dedup/labeling'),
  newDedupTaxonomy: def('/new-dedup/labeling/taxonomy'),
  newDedupExam: def('/new-dedup/exam'),
  newDedupExamReview: def('/new-dedup/exam/review'),
  scrapers: def('/scrapers'),
  devConfidenceIndicator: def('/dev/confidence-indicator'),
} as const;

export type RouteKey = keyof typeof ROUTES;

/* Append a query string, skipping null/undefined so a caller can pass a sparse
 * record without pre-filtering. Values are encoded by URLSearchParams. */
export function withQuery(
  path: RoutePath,
  query: Record<string, string | number | boolean | null | undefined>,
): RoutePath {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v == null) continue;
    sp.set(k, String(v));
  }
  const qs = sp.toString();
  return (qs ? `${path}?${qs}` : path) as RoutePath;
}

/* Append a fragment. `''` is a valid, meaningful argument — runLinks passes it
 * to mean "this hash has no anchor on the target page, drop it". */
export function withHash(path: RoutePath, hash: string): RoutePath {
  return (hash ? `${path}${hash}` : path) as RoutePath;
}
