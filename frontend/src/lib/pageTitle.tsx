/* The one place document.title is formed and written. Every browser tab title
 * flows through here so the app has a single, consistent naming scheme.
 *
 * Two inputs, one writer:
 *   - Static pages declare a fixed name via the route's `handle.title` in
 *     routes.tsx (single source of truth, co-located with the route).
 *   - Dynamic pages call `usePageTitle(segment)` to override with per-entity or
 *     per-filter text (a listing address, a Browse filter summary, …).
 * `TitleController` (mounted once in App) computes the final title as
 * `override ?? handleTitle` and is the ONLY code that assigns document.title —
 * so the two inputs can never fight and no page can leak a stale title.
 *
 * Router note: the app uses <BrowserRouter> + useRoutes (not the data router),
 * so route `handle`s are read with the standalone `matchRoutes`, not
 * `useMatches`. No router migration required.
 */
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { matchRoutes, useLocation, type RouteObject } from 'react-router-dom';
import { APP_NAME, APP_SHORT } from './brand';

// Re-exported so callers can keep importing brand from the title module; the
// canonical definition lives in ./brand (shared with the Chrome extension).
export { APP_NAME, APP_SHORT };

/* Shape of the `handle` we read off routes. Kept permissive so routes without a
 * title (dynamic pages, redirects) simply contribute nothing. */
export interface RouteTitleHandle {
  title?: string;
}

/** "LR: <segment>", or the bare app name when there is no segment. */
export function formatTitle(segment: string | null | undefined): string {
  const trimmed = segment?.trim();
  return trimmed ? `${APP_SHORT}: ${trimmed}` : APP_NAME;
}

/* An override is keyed by the path that set it, so a value left behind by a
 * page that is unmounting is ignored the instant the location changes — the new
 * route falls back to its handle title for that frame instead of flashing the
 * previous page's title. */
interface TitleOverride {
  path: string;
  segment: string;
}

type SetOverride = (next: TitleOverride | null) => void;

const SetTitleContext = createContext<SetOverride>(() => {});

/* Walk the matched route chain (deepest wins) for a handle title. */
function handleTitleFor(routes: RouteObject[], pathname: string): string | null {
  const matches = matchRoutes(routes, pathname);
  if (!matches) return null;
  let title: string | null = null;
  for (const m of matches) {
    const h = m.route.handle as RouteTitleHandle | undefined;
    if (h?.title) title = h.title;
  }
  return title;
}

/**
 * Mount once, inside the router. Owns document.title. Computes
 * `override(for this path) ?? routeHandleTitle` and writes it formatted.
 */
export function TitleController({
  routes,
  children,
}: {
  routes: RouteObject[];
  children: ReactNode;
}) {
  const { pathname } = useLocation();
  const [override, setOverride] = useState<TitleOverride | null>(null);

  const handleTitle = useMemo(
    () => handleTitleFor(routes, pathname),
    [routes, pathname],
  );

  const segment =
    override && override.path === pathname ? override.segment : handleTitle;

  useEffect(() => {
    document.title = formatTitle(segment);
  }, [segment]);

  return (
    <SetTitleContext.Provider value={setOverride}>
      {children}
    </SetTitleContext.Provider>
  );
}

/**
 * Set the current tab title from inside a page. Pass a string to override the
 * route's static title (use for per-entity / per-filter pages); pass null while
 * data is still loading to fall back to the route's handle title. The override
 * is automatically released when the page unmounts.
 *
 * A null argument is a genuine no-op — it contributes nothing and never clears
 * an override set elsewhere. (When a value becomes null the previous run's
 * cleanup still releases it, so a page that loses its data correctly falls back
 * to the handle title; and a component that ALWAYS passes null — e.g.
 * BrowseExperience inside the Explore-area modal — never disturbs the page that
 * owns the title.)
 */
export function usePageTitle(segment: string | null): void {
  const setOverride = useContext(SetTitleContext);
  const { pathname } = useLocation();
  const trimmed = segment?.trim() ? segment.trim() : null;

  useEffect(() => {
    if (trimmed == null) return;
    setOverride({ path: pathname, segment: trimmed });
    return () => setOverride(null);
  }, [setOverride, pathname, trimmed]);
}
