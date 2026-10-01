/* App-wide mutation-failure surfacing: any mutation that does NOT define its
 * own onError gets its error toasted here, so no write ever fails silently
 * (e.g. a refused merge returning HTTP 409). Mutations with their own onError
 * own their messaging and are left untouched — no double-surfacing.
 *
 * A factory rather than a main.tsx local so a test can build the client the
 * app runs with and see the one toast a failed write produces. */

import { MutationCache } from '@tanstack/react-query';

import { ApiError } from '@/lib/api';
import { pushToast } from '@/lib/toast';

export function createMutationCache(): MutationCache {
  return new MutationCache({
    onError: (error, _variables, _context, mutation) => {
      if (mutation.options.onError) return;
      const message =
        error instanceof ApiError || error instanceof Error
          ? error.message
          : 'Something went wrong';
      pushToast('err', message);
    },
  });
}
