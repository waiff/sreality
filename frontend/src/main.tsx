import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { MutationCache, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import App from './App';
import { AuthProvider } from './lib/auth';
import { ApiError, isTransientApiError } from './lib/api';
import { pushToast } from './lib/toast';
import { applyTheme, readStoredTheme } from './lib/theme';
import './styles/globals.css';

applyTheme(readStoredTheme());

/* App-wide mutation-failure surfacing: any mutation that does NOT define its
 * own onError gets its error toasted here, so no write ever fails silently
 * (e.g. a refused merge returning HTTP 409). Mutations with their own onError
 * own their messaging and are left untouched — no double-surfacing. */
const mutationCache = new MutationCache({
  onError: (error, _variables, _context, mutation) => {
    if (mutation.options.onError) return;
    const message =
      error instanceof ApiError || error instanceof Error
        ? error.message
        : 'Something went wrong';
    pushToast('err', message);
  },
});

const queryClient = new QueryClient({
  mutationCache,
  defaultOptions: {
    queries: {
      staleTime: 60_000,
      gcTime: 5 * 60_000,
      refetchOnWindowFocus: false,
      /* One more attempt ONLY when it can help: a network blip or a 502/503/504
       * (the API answers a busy database with 503 db_busy). Never a 4xx, a 500
       * or a transport-deadline timeout — those are deterministic, and the old
       * blanket `retry: 1` turned each of them into a double-length silent
       * spinner (the 2026-09-30 Brokers hang was 2 × 120 s exactly this way). */
      retry: (failureCount, error) => failureCount < 1 && isTransientApiError(error),
    },
  },
});

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <App />
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
