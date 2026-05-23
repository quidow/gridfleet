import { usePageTitle } from '../hooks/usePageTitle';
import { SectionErrorBoundary } from '../components/ErrorBoundary';
import { DashboardHeader } from '../components/dashboard/DashboardHeader';
import { StatCardsRow } from '../components/dashboard/StatCardsRow';
import { FleetByPlatformCard } from '../components/dashboard/FleetByPlatformCard';
import { RecentIncidentsCard } from '../components/dashboard/RecentIncidentsCard';
import { OperationsSection } from '../components/dashboard/OperationsSection';

export function Dashboard() {
  usePageTitle('Dashboard');

  return (
    <div>
      <SectionErrorBoundary scope="dashboard-header">
        <DashboardHeader />
      </SectionErrorBoundary>
      <div className="fade-in-stagger flex flex-col gap-6">
        <SectionErrorBoundary scope="stat-cards">
          <StatCardsRow />
        </SectionErrorBoundary>
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3 lg:items-stretch">
          <div className="lg:col-span-2">
            <SectionErrorBoundary scope="fleet-by-platform">
              <FleetByPlatformCard />
            </SectionErrorBoundary>
          </div>
          <SectionErrorBoundary scope="recent-incidents">
            <RecentIncidentsCard />
          </SectionErrorBoundary>
        </div>
        <SectionErrorBoundary scope="operations">
          <OperationsSection />
        </SectionErrorBoundary>
      </div>
    </div>
  );
}
