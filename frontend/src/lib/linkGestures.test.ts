import { describe, expect, it } from 'vitest';
import { isPlainLeftClick, spaNavHrefForClick } from './linkGestures';

/* A minimal stand-in for the anchor ancestry the predicate walks. Structural
 * typing is the point: no DOM event constructor, no map, no mount. */
function clickOn(
  anchorAttrs: Record<string, string> | null,
  over: Record<string, unknown> = {},
) {
  const anchor = anchorAttrs && {
    getAttribute: (n: string) => anchorAttrs[n] ?? null,
    hasAttribute: (n: string) => n in anchorAttrs,
  };
  return {
    button: 0,
    metaKey: false,
    ctrlKey: false,
    shiftKey: false,
    altKey: false,
    defaultPrevented: false,
    // The real click target is usually a <span> INSIDE the anchor — the
    // "View details →" text — so the walk has to go up.
    target: { closest: (s: string) => (s === 'a' ? anchor : null) },
    ...over,
  };
}

describe('isPlainLeftClick', () => {
  it('accepts an unmodified primary click', () => {
    expect(isPlainLeftClick(clickOn(null))).toBe(true);
  });

  it.each(['metaKey', 'ctrlKey', 'shiftKey', 'altKey'])('rejects %s', (mod) => {
    expect(isPlainLeftClick(clickOn(null, { [mod]: true }))).toBe(false);
  });

  it('rejects middle and right buttons', () => {
    expect(isPlainLeftClick(clickOn(null, { button: 1 }))).toBe(false);
    expect(isPlainLeftClick(clickOn(null, { button: 2 }))).toBe(false);
  });

  it('rejects a click something else already handled', () => {
    expect(isPlainLeftClick(clickOn(null, { defaultPrevented: true }))).toBe(false);
  });
});

describe('spaNavHrefForClick', () => {
  it('routes a plain click on a child of an in-app anchor', () => {
    expect(spaNavHrefForClick(clickOn({ href: '/listing/bazos/abc-1' }))).toBe(
      '/listing/bazos/abc-1',
    );
  });

  it.each(['metaKey', 'ctrlKey', 'shiftKey', 'altKey'])(
    'leaves a %s click to the browser, so the tab/download gesture survives',
    (mod) => {
      expect(spaNavHrefForClick(clickOn({ href: '/browse' }, { [mod]: true }))).toBeNull();
    },
  );

  it('leaves middle and right clicks to the browser', () => {
    expect(spaNavHrefForClick(clickOn({ href: '/browse' }, { button: 1 }))).toBeNull();
    expect(spaNavHrefForClick(clickOn({ href: '/browse' }, { button: 2 }))).toBeNull();
  });

  it('ignores a click with no anchor ancestor', () => {
    expect(spaNavHrefForClick(clickOn(null))).toBeNull();
  });

  it('ignores an explicit download', () => {
    expect(spaNavHrefForClick(clickOn({ href: '/report.csv', download: '' }))).toBeNull();
  });

  it('respects target', () => {
    expect(spaNavHrefForClick(clickOn({ href: '/browse', target: '_blank' }))).toBeNull();
    expect(spaNavHrefForClick(clickOn({ href: '/browse', target: '_self' }))).toBe('/browse');
  });

  it('leaves external destinations alone — this is what guards the map attribution', () => {
    expect(spaNavHrefForClick(clickOn({ href: 'https://openfreemap.org/' }))).toBeNull();
    expect(spaNavHrefForClick(clickOn({ href: 'mailto:a@b.cz' }))).toBeNull();
    expect(spaNavHrefForClick(clickOn({ href: '#anchor' }))).toBeNull();
  });

  it('does NOT capture a protocol-relative URL, which is external despite the slash', () => {
    expect(spaNavHrefForClick(clickOn({ href: '//evil.example/x' }))).toBeNull();
  });

  it('ignores an anchor with no href', () => {
    expect(spaNavHrefForClick(clickOn({}))).toBeNull();
  });
});
