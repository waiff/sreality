/* AUTODEDUP · the one box a review page says "nothing to show" in: an empty
 * filter, a pass with nothing in it, a store the database does not hold yet. */

import { type ReactNode } from 'react';

export default function Notice({ children }: { children: ReactNode }) {
  return (
    <p className="mt-6 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4 text-sm text-[var(--color-ink-2)]">
      {children}
    </p>
  );
}

/* Migration 528's store is not in this database: every review route answers
 * `store_ready: false`, and every page says the same sentence about it. */
export function StoreNotReady() {
  return (
    <Notice>
      Úložiště programu v této databázi zatím není, takže tu zatím nic není ke kontrole.
    </Notice>
  );
}
