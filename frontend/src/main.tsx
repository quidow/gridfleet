import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import { AuthProvider } from './context/AuthContext';
import { ThemeProvider } from './context/ThemeContext';
import './index.css';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Throw fetch errors into the nearest ErrorBoundary by default so the
      // boundary owns fetch-error UI (per FRONTEND_BEST_PRACTICES.md). Hooks
      // that want to display errors inline opt out with
      // `meta: { handleErrorLocally: true }`.
      throwOnError: (_error, query) =>
        !(query.meta && (query.meta as { handleErrorLocally?: boolean }).handleErrorLocally),
      staleTime: 5_000,
      retry: 1,
      refetchOnWindowFocus: true,
    },
  },
});

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <ThemeProvider>
          <AuthProvider>
            <App />
          </AuthProvider>
        </ThemeProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
