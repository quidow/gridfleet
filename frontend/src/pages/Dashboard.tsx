import { usePageTitle } from '../hooks/usePageTitle';
import { SectionErrorBoundary } from '../components/ErrorBoundary';
import { DashboardHeader } from '../components/dashboard/DashboardHeader';
import { Scorecard } from '../components/dashboard/Scorecard';
import { FleetCard } from '../components/dashboard/FleetCard';
import { AttentionCard } from '../components/dashboard/AttentionCard';
import { ActivityCard } from '../components/dashboard/ActivityCard';

export function Dashboard() {
  usePageTitle('Dashboard');

  return (
    <div>
      <SectionErrorBoundary scope="dashboard-header">
        <DashboardHeader />
      </SectionErrorBoundary>
      <div className="fade-in-stagger flex flex-col gap-4">
        <SectionErrorBoundary scope="scorecard">
          <Scorecard />
        </SectionErrorBoundary>
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3 lg:items-stretch">
          <div className="lg:col-span-2">
            <SectionErrorBoundary scope="fleet">
              <FleetCard />
            </SectionErrorBoundary>
          </div>
          <SectionErrorBoundary scope="attention">
            <AttentionCard />
          </SectionErrorBoundary>
        </div>
        <SectionErrorBoundary scope="activity">
          <ActivityCard />
        </SectionErrorBoundary>
      </div>
    </div>
  );
}
