/* The anti-404 rail.
 *
 * `routes.tsx` CONSUMES `ROUTES` (each route's `path` is a `.childPath` /
 * `.pattern`), so "do the two tables agree" is not a question this file has to
 * ask — there is one table. What it does ask is the question that survives that
 * design: does every pattern in the registry actually RESOLVE against the
 * router, to the route it names, rather than falling through to the `*`
 * catch-all? A path that renders NotFound does so with an HTTP 200 (the host
 * serves the SPA at every depth), so nothing else in the stack can catch it. */
import { describe, expect, it } from 'vitest';
import { matchRoutes } from 'react-router-dom';
import { routes } from '../routes';
import { ROUTES, withHash, withQuery, type RouteKey } from './routes';

/* Structural routes, deliberately absent from the registry: the Shell's `/`
 * parent, the catch-all, and `estimate` — a bare legacy redirect to
 * /estimations that is not a destination and has zero references. */
const NOT_DESTINATIONS = new Set(['/', '*', 'estimate']);

/* One fixture value per param name in the registry. A new `:param` with no
 * fixture fails `builds every pattern` below rather than silently skipping. */
const PARAM_FIXTURES: Record<string, string | number> = {
  id: 7,
  source: 'bazos',
  nativeId: 'abc-123',
  sreality_id: -284913,
};

function paramNames(pattern: string): string[] {
  return (pattern.match(/:[A-Za-z_][A-Za-z0-9_]*/g) ?? []).map((p) => p.slice(1));
}

function fixtureFor(pattern: string): Record<string, string | number> {
  const out: Record<string, string | number> = {};
  for (const n of paramNames(pattern)) {
    if (!(n in PARAM_FIXTURES)) throw new Error(`no fixture for :${n} (pattern ${pattern})`);
    out[n] = PARAM_FIXTURES[n];
  }
  return out;
}

/* Join a nested RouteObject tree into the absolute patterns it declares. */
function declaredPatterns(
  nodes: ReadonlyArray<{ path?: string; index?: boolean; children?: ReadonlyArray<unknown> }>,
  parent = '',
): string[] {
  const out: string[] = [];
  for (const n of nodes) {
    if (n.index) continue;
    const raw = n.path ?? '';
    const abs = raw.startsWith('/') ? raw : `${parent === '/' ? '' : parent}/${raw}`;
    if (!NOT_DESTINATIONS.has(raw)) out.push(abs);
    if (n.children) {
      out.push(
        ...declaredPatterns(
          n.children as ReadonlyArray<{ path?: string; children?: ReadonlyArray<unknown> }>,
          raw.startsWith('/') ? raw : abs,
        ),
      );
    }
  }
  return out;
}

const KEYS = Object.keys(ROUTES) as RouteKey[];

describe('ROUTES resolves against the real router table', () => {
  it.each(KEYS)('%s builds a path that matches its own pattern, not the catch-all', (key) => {
    const def = ROUTES[key];
    const built = (def.build as (p?: Record<string, string | number>) => string)(
      fixtureFor(def.pattern),
    );
    const matched = matchRoutes(routes, built);
    expect(matched, `${key} (${built}) matched nothing`).not.toBeNull();

    const deepest = matched![matched!.length - 1].route as { path?: string };
    expect(deepest.path, `${key} (${built}) fell through to the catch-all`).not.toBe('*');

    // The matched leaf is this entry's own pattern, in whichever form
    // routes.tsx declared it (absolute for the auth screens, relative under
    // the Shell parent for everything else).
    expect([def.pattern, def.childPath]).toContain(deepest.path);
  });
});

describe('every destination the router declares has a registry entry', () => {
  it('leaves no route reachable only by a hand-typed URL', () => {
    const declared = declaredPatterns(
      routes as ReadonlyArray<{ path?: string; children?: ReadonlyArray<unknown> }>,
    );
    const registered = new Set(KEYS.map((k) => ROUTES[k].pattern as string));
    const orphans = declared.filter((p) => !registered.has(p));
    expect(orphans).toEqual([]);
  });
});

describe('build()', () => {
  it('percent-encodes params, because generatePath does not', () => {
    // A portal's native id is free-form text. Unencoded, a "/" would change the
    // route's shape and resolve somewhere else entirely.
    expect(ROUTES.listingCanonical.build({ source: 'idnes', nativeId: 'a/b' })).toBe(
      '/listing/idnes/a%2Fb',
    );
    expect(ROUTES.listingCanonical.build({ source: 'x y', nativeId: 'a?b#c' })).toBe(
      '/listing/x%20y/a%3Fb%23c',
    );
  });

  it('keeps a negative synthetic sreality_id intact (migration 097)', () => {
    expect(ROUTES.listingLegacy.build({ sreality_id: -284913 })).toBe('/listing/-284913');
  });

  it('accepts numbers as well as strings', () => {
    expect(ROUTES.brokerDetail.build({ id: 7 })).toBe('/brokers/7');
    expect(ROUTES.brokerDetail.build({ id: '7' })).toBe('/brokers/7');
  });

  it('derives childPath from the pattern', () => {
    expect(ROUTES.brokerDetail.childPath).toBe('brokers/:id');
    expect(ROUTES.browse.childPath).toBe('browse');
  });
});

describe('withQuery / withHash', () => {
  it('skips null and undefined so callers can pass a sparse record', () => {
    expect(withQuery(ROUTES.browse.build(), { a: 1, b: null, c: undefined })).toBe('/browse?a=1');
  });

  it('returns the bare path when every value is absent', () => {
    expect(withQuery(ROUTES.browse.build(), { b: null })).toBe('/browse');
  });

  it('treats an empty hash as "drop it"', () => {
    expect(withHash(ROUTES.estimationDetail.build({ id: 3 }), '')).toBe('/estimation/3');
    expect(withHash(ROUTES.estimationDetail.build({ id: 3 }), '#feedback')).toBe(
      '/estimation/3#feedback',
    );
  });
});
