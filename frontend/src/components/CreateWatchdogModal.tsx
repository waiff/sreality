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

import { useEffect, useId, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ROUTES } from '@/lib/routes';
import { useMutation } from '@tanstack/react-query';
import { ApiError, createWatchdogSubscription } from '@/lib/api';
import type { WatchdogFilterSpec } from '@/lib/types';
import Dialog from '@/components/Dialog';
import { DeliveryChannelsPicker } from '@/components/DeliveryChannelsPicker';

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
  const [channels, setChannels] = useState<string[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);
  const titleId = useId();

  /* Focus is the primitive's (`initialFocus` below). What this adds is the
   * SELECTION, so the suggested name can be typed straight over. Mount-only:
   * a dependency here is the focus-theft bug lib/useDialog describes. */
  useEffect(() => {
    inputRef.current?.select();
  }, []);

  const createMut = useMutation({
    mutationFn: (watchdogName: string) =>
      createWatchdogSubscription({
        name: watchdogName,
        filter_spec: spec,
        is_active: true,
        channels,
      }),
    onSuccess: () => {
      navigate(ROUTES.watchdogManage.build());
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

  /* Escape, the focus trap, initial + restored focus, the backdrop click and
   * the body scroll lock all come from <Dialog> (lib/useDialog.ts). The
   * hand-rolled window listener that used to live here is gone, and so is the
   * panel's `onClick={(e) => e.stopPropagation()}` — that existed only to stop
   * the backdrop's own `onClick={onClose}` from firing on a click INSIDE the
   * dialog, and <Dialog> makes the target test instead of asking the panel to
   * swallow every click that reaches it.
   *
   * The name is now the VISIBLE heading rather than the literal "Create
   * watchdog" the old aria-label carried, so what a screen reader announces is
   * what the operator reads. */
  return (
    <Dialog open onClose={onClose} labelledBy={titleId} initialFocus={inputRef} className="w-full max-w-md p-5">
      <p className="text-[0.7rem] tracking-[0.18em] uppercase text-[var(--color-ink-3)]">
        Watchdog
      </p>
      <h2
        id={titleId}
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
          className="mt-1 w-full px-3 py-2 text-sm rounded-[var(--radius-sm)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] text-[var(--color-ink)] focus-visible:border-[var(--color-copper)]"
        />
      </label>

      <div className="mt-4">
        <span className="text-[0.65rem] tracking-[0.14em] uppercase text-[var(--color-ink-4)]">
          Delivery
        </span>
        <div className="mt-1.5">
          <DeliveryChannelsPicker value={channels} onChange={setChannels} />
        </div>
      </div>

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
    </Dialog>
  );
}
