import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import App from './App';
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

const queryClient = new QueryClient({
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
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
