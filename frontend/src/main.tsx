import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import App from './App';
import { AuthProvider } from './lib/auth';
import { isTransientApiError } from './lib/api';
import { createMutationCache } from './lib/mutationCache';
import { applyTheme, readStoredTheme } from './lib/theme';
import './styles/globals.css';

applyTheme(readStoredTheme());

/* The global "a write failed" toast — see lib/mutationCache. */
const mutationCache = createMutationCache();

const queryClient = new QueryClient({
  mutationCache,
  defaultOptions: {
    queries: {
      staleTime: 60_000,
      gcTime: 5 * 60_000,
      refetchOnWindowFocus: false,
      /* One more attempt ONLY when it can help: a network blip or a
       * 502/503/504/520 (the API answers a busy database with 503 db_busy;
       * 520 is Cloudflare in front of Supabase). Never a 4xx, a 500 or a
       * transport-deadline timeout — those are deterministic, and the old
       * blanket `retry: 1` turned each of them into a double-length silent
       * spinner (the 2026-09-30 Brokers hang was 2 × 120 s exactly this way).
       * ONE rule for both adapters: lib/api.ts and lib/pgRead.ts (every
       * PostgREST read) both throw ApiError, and pgRead switches postgrest-js's
       * own hidden retry off, so a busy or unreachable PostgREST read retries
       * here exactly like a FastAPI one — and nowhere else. */
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
