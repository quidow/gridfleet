import { Link } from 'react-router-dom';
import { Package } from 'lucide-react';
import Card from '../ui/Card';
import SettingsPanelLayout from './SettingsPanelLayout';
import { useDriverPackCatalog } from '../../hooks/useDriverPacks';

export default function DriverPackPanel() {
  const { data } = useDriverPackCatalog();
  const count = data?.length ?? 0;

  return (
    <SettingsPanelLayout
      title="Drivers"
      description="Driver packs are now managed from their own section."
    >
      <Card padding="md" className="flex flex-col gap-4 sm:flex-row sm:items-center">
        <Package size={24} className="shrink-0 text-text-3" />
        <div className="flex-1">
          <p className="font-medium text-text-1">
            {count} driver pack{count !== 1 ? 's' : ''} installed
          </p>
          <p className="text-sm text-text-3">
            Upload, configure, and inspect driver packs from the Drivers page.
          </p>
        </div>
        <Link
          to="/drivers"
          className="inline-flex shrink-0 items-center justify-center rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-on hover:bg-accent-hover"
        >
          View All Driver Packs
        </Link>
      </Card>
    </SettingsPanelLayout>
  );
}
