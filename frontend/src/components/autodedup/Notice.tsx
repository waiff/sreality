/* AUTODEDUP · the one box a review page says "nothing to show" in. */

import { type ReactNode } from 'react';

export default function Notice({ children }: { children: ReactNode }) {
  return (
    <p className="mt-6 rounded-[var(--radius-md)] border border-[var(--color-rule)] bg-[var(--color-paper-2)] px-5 py-4 text-sm text-[var(--color-ink-2)]">
      {children}
    </p>
  );
}

/* `store_ready: false`: migration 528's store is not in this database. */
export function StoreNotReady() {
  return (
    <Notice>
      Úložiště programu v této databázi zatím není, takže tu zatím nic není ke kontrole.
    </Notice>
  );
}
