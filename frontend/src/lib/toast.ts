/* App-wide transient feedback (toasts).
 *
 * A tiny framework-agnostic pub/sub store so non-React code — notably the
 * QueryClient's global MutationCache.onError in main.tsx — can surface a
 * message without a React context. Components read it via useToasts()
 * (useSyncExternalStore). This is the single toast primitive; pages must not
 * hand-roll their own (see ToastViewport). */

import { useSyncExternalStore } from 'react';

export type ToastKind = 'ok' | 'err' | 'info';

export interface Toast {
  id: number;
  kind: ToastKind;
  message: string;
}

const DEFAULT_TTL_MS = 6000;

let toasts: ReadonlyArray<Toast> = [];
let nextId = 1;
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

export function pushToast(
  kind: ToastKind,
  message: string,
  ttlMs: number = DEFAULT_TTL_MS,
): number {
  const id = nextId++;
  toasts = [...toasts, { id, kind, message }];
  emit();
  if (ttlMs > 0) {
    setTimeout(() => dismissToast(id), ttlMs);
  }
  return id;
}

export function dismissToast(id: number): void {
  const next = toasts.filter((t) => t.id !== id);
  if (next.length === toasts.length) return;
  toasts = next;
  emit();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function getSnapshot(): ReadonlyArray<Toast> {
  return toasts;
}

export function useToasts(): ReadonlyArray<Toast> {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
