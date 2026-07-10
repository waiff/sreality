import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { MutationCache, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import App from './App';
import { AuthProvider } from './lib/auth';
import { ApiError } from './lib/api';
import { pushToast } from './lib/toast';
import { applyTheme, readStoredTheme } from './lib/theme';
import './styles/globals.css';

const CHUNK_RELOAD_FLAG = 'sreality:chunkReloadAttempted';

window.addEventListener('vite:preloadError', (event) => {
  if (sessionStorage.getItem(CHUNK_RELOAD_FLAG) === '1') return;
  sessionStorage.setItem(CHUNK_RELOAD_FLAG, '1');
  event.preventDefault();
  window.location.reload();
});

window.addEventListener('load', () => {
  sessionStorage.removeItem(CHUNK_RELOAD_FLAG);
});

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
      retry: 1,
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
