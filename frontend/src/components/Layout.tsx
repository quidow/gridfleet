import { Suspense } from 'react';
import { Outlet, useLocation } from 'react-router-dom';
import { Toaster } from 'sonner';
import Sidebar from './Sidebar';
import { useEventStream } from '../hooks/useEventStream';
import { EventStreamContext } from '../context/EventStreamContext';
import LoadingSpinner from './LoadingSpinner';
import SidebarProvider from './SidebarProvider';
import { PageErrorBoundary } from './ErrorBoundary';

export default function Layout() {
  const { connected } = useEventStream();
  const location = useLocation();

  return (
    <EventStreamContext.Provider value={{ connected }}>
      <SidebarProvider>
        <div className="flex h-screen bg-surface-0 text-text-1">
          <Sidebar />
          <main className="flex-1 overflow-auto">
            <div className="page-gutter min-h-full">
              <PageErrorBoundary resetKey={location.pathname} scope="route-outlet">
                <Suspense fallback={<LoadingSpinner />}>
                  <Outlet />
                </Suspense>
              </PageErrorBoundary>
            </div>
          </main>
          <Toaster position="top-right" richColors closeButton />
        </div>
      </SidebarProvider>
    </EventStreamContext.Provider>
  );
}
