import { usePageTitle } from '../hooks/usePageTitle';
import DashboardHeader from '../components/dashboard/DashboardHeader';
import StatCardsRow from '../components/dashboard/StatCardsRow';
import FleetByPlatformCard from '../components/dashboard/FleetByPlatformCard';
import RecentIncidentsCard from '../components/dashboard/RecentIncidentsCard';
import OperationsSection from '../components/dashboard/OperationsSection';

export default function Dashboard() {
  usePageTitle('Dashboard');

  return (
    <div>
      <DashboardHeader />
      <div className="fade-in-stagger flex flex-col gap-6">
        <StatCardsRow />
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3 lg:items-stretch">
          <div className="lg:col-span-2">
            <FleetByPlatformCard />
          </div>
          <RecentIncidentsCard />
        </div>
        <OperationsSection />
      </div>
    </div>
  );
}
