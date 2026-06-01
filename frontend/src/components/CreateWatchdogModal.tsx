/* "Create watchdog from Browse" — a small name-prompt dialog.
 *
 * The Browse page builds a WatchdogFilterSpec from the current filters
 * (filtersToWatchdogSpec) and opens this modal so the operator can name the
 * saved filter before it's persisted. On confirm we POST
 * /notifications/subscriptions; on success the watchdog shows up in the
 * Watchdog feed / Manage list like any other.
 *
 * Self-contained (no context provider): Browse owns the open/close state and
 * passes the prepared spec in. Any Browse filters the matcher can't honour are
 * surfaced as a heads-up so the operator isn't surprised. */

import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation } from '@tanstack/react-query';
import { ApiError, createWatchdogSubscription } from '@/lib/api';
import type { WatchdogFilterSpec } from '@/lib/types';

export interface CreateWatchdogModalProps {
  spec: WatchdogFilterSpec;
  /* Set-but-unmonitored Browse filters (from filtersToWatchdogSpec). */
  unsupported: string[];
  /* A suggested default name derived from the active filters. */
  suggestedName: string;
  onClose: () => void;
}

export default function CreateWatchdogModal({
  spec,
  unsupported,
  suggestedName,
  onClose,
}: CreateWatchdogModalProps) {
  const navigate = useNavigate();
  const [name, setName] = useState(suggestedName);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const createMut = useMutation({
    mutationFn: (watchdogName: string) =>
      createWatchdogSubscription({
        name: watchdogName,
        filter_spec: spec,
        is_active: true,
      }),
    onSuccess: () => {
      navigate('/watchdog/manage');
    },
  });

  const trimmed = name.trim();
  const submit = () => {
    if (trimmed.length === 0 || createMut.isPending) return;
    createMut.mutate(trimmed);
  };

  const errMsg =
    createMut.error instanceof ApiError
      ? createMut.error.message
      : createMut.error
        ? 'Something went wrong.'
        : null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-[var(--color-ink)]/40 px-4 pt-[15vh]"
      onClick={onClose}
      role="presentation"
    >
      <div
        className="w-full max-w-md rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper)] p-5 shadow-lg"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Create watchdog"
      >
        <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
          Watchdog
        </p>
        <h2
          className="mt-1 text-xl leading-tight"
          style={{ fontFamily: 'var(--font-display)', fontWeight: 600 }}
        >
          Save these filters as a watchdog
        </h2>
        <p className="mt-2 text-sm text-[var(--color-ink-2)]">
          You'll get a notification the moment a freshly scraped listing matches
          this filter set.
        </p>

        <label className="mt-4 block">
          <span className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
            Watchdog name
          </span>
          <input
            ref={inputRef}
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') submit();
            }}
            placeholder="e.g. 2+kk Praha pod 6M"
            className="mt-1 w-full px-3 py-2 text-sm rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[var(--color-ink)] focus:outline-none focus:border-[var(--color-copper)]"
          />
        </label>

        {unsupported.length > 0 ? (
          <div className="mt-3 rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-3 py-2">
            <p className="text-[0.7rem] text-[var(--color-ink-2)]">
              These active filters can't be watched and will be left off:{' '}
              <span className="text-[var(--color-ink)]">
                {unsupported.join(', ')}
              </span>
              . A watchdog fires on brand-new listings, so date / status / map-area
              filters don't apply.
            </p>
          </div>
        ) : null}

        {errMsg ? (
          <p className="mt-3 text-[0.8rem] text-[var(--color-brick)]">{errMsg}</p>
        ) : null}

        <div className="mt-5 flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="px-3 py-1.5 text-sm rounded-[var(--radius-sm)] border border-[var(--color-rule)] text-[var(--color-ink-2)] hover:text-[var(--color-ink)] hover:border-[var(--color-rule-strong)] transition-colors"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={submit}
            disabled={trimmed.length === 0 || createMut.isPending}
            className="px-3 py-1.5 text-sm rounded-[var(--radius-sm)] bg-[var(--color-copper)] text-white hover:bg-[var(--color-copper-2)] transition-colors disabled:opacity-50"
          >
            {createMut.isPending ? 'Creating…' : 'Create watchdog'}
          </button>
        </div>
      </div>
    </div>
  );
}
