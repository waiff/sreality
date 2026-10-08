import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { readFlag, useMapCollapsed } from './browseLayout';

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
