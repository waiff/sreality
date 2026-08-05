import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { readFlag, useGrainNotice, useMapCollapsed } from './browseLayout';

const KEY = 'sreality.browse.mapCollapsed';

describe('useMapCollapsed', () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => localStorage.clear());

  it('defaults to false (map shown) when nothing is stored', () => {
    const { result } = renderHook(() => useMapCollapsed());
    expect(result.current.value).toBe(false);
  });

  it('set() updates state and persists', () => {
    const { result } = renderHook(() => useMapCollapsed());
    act(() => result.current.set(true));
    expect(result.current.value).toBe(true);
    expect(localStorage.getItem(KEY)).toBe('1');
    act(() => result.current.set(false));
    expect(result.current.value).toBe(false);
    expect(localStorage.getItem(KEY)).toBe('0');
  });

  it('toggle() flips and persists (no stale closure across calls)', () => {
    const { result } = renderHook(() => useMapCollapsed());
    act(() => result.current.toggle());
    expect(result.current.value).toBe(true);
    act(() => result.current.toggle());
    expect(result.current.value).toBe(false);
    expect(localStorage.getItem(KEY)).toBe('0');
  });

  it('reads a persisted value on mount', () => {
    localStorage.setItem(KEY, '1');
    const { result } = renderHook(() => useMapCollapsed());
    expect(result.current.value).toBe(true);
  });
});

describe('useGrainNotice', () => {
  const MIRROR = 'sreality.browse.grainNoticeDismissed.mirror';
  const MERGED = 'sreality.browse.grainNoticeDismissed.merged';

  beforeEach(() => localStorage.clear());
  afterEach(() => localStorage.clear());

  it('shows both variants until each is dismissed', () => {
    const { result } = renderHook(() => useGrainNotice());
    expect(result.current.dismissed('mirror')).toBe(false);
    expect(result.current.dismissed('merged')).toBe(false);
  });

  /* The whole point of two keys: the merged note is on the default view and
   * gets dismissed early, but the mirror note explains the OPPOSITE row grain
   * and must still appear the first time a single portal is picked. */
  it('keeps the two variants independent', () => {
    const { result } = renderHook(() => useGrainNotice());
    act(() => result.current.dismiss('merged'));
    expect(result.current.dismissed('merged')).toBe(true);
    expect(result.current.dismissed('mirror')).toBe(false);
    expect(localStorage.getItem(MERGED)).toBe('1');
    expect(localStorage.getItem(MIRROR)).toBe(null);
  });

  it('reads persisted dismissals on mount', () => {
    localStorage.setItem(MIRROR, '1');
    const { result } = renderHook(() => useGrainNotice());
    expect(result.current.dismissed('mirror')).toBe(true);
    expect(result.current.dismissed('merged')).toBe(false);
  });
});

describe('readFlag', () => {
  beforeEach(() => localStorage.clear());

  it('returns the fallback when unset and parses "1"/"0"', () => {
    expect(readFlag('x', false)).toBe(false);
    expect(readFlag('x', true)).toBe(true);
    localStorage.setItem('x', '1');
    expect(readFlag('x', false)).toBe(true);
    localStorage.setItem('x', '0');
    expect(readFlag('x', true)).toBe(false);
  });
});
