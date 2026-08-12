/* Which top-level nav entry owns the current path.
 *
 * `/brokers/review` (added 2026-08-12) is the first nav entry nested under
 * another one. NavLink's default prefix matching lights a parent on any
 * descendant path, so without this both "Brokers" and "Broker Review" would
 * render active at once; exact-matching the parent instead would drop the
 * highlight on /brokers/123. Longest match wins, which does both.
 */

import { describe, expect, it } from 'vitest';

import { activeNavTo } from './Shell';

const TOS = ['/browse', '/brokers', '/brokers/review', '/collections'];

describe('activeNavTo', () => {
  it('gives a nested path to its most specific entry', () => {
    expect(activeNavTo('/brokers/review', TOS)).toBe('/brokers/review');
  });

  it('keeps a non-nav descendant on its parent entry', () => {
    expect(activeNavTo('/brokers/123', TOS)).toBe('/brokers');
    expect(activeNavTo('/brokers', TOS)).toBe('/brokers');
  });

  it('is order-independent', () => {
    expect(activeNavTo('/brokers/review', [...TOS].reverse())).toBe('/brokers/review');
  });

  it('is null on a path no entry owns', () => {
    expect(activeNavTo('/settings', TOS)).toBeNull();
    // A prefix that is not a path segment boundary must not match.
    expect(activeNavTo('/brokers-archive', TOS)).toBeNull();
  });
});
