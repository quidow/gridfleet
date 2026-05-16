import { useState } from 'react';
import { Link } from 'react-router-dom';
import { Package } from 'lucide-react';
import { Badge, Button, DataTable, EmptyState, PageHeader, type DataTableColumn } from '../components/ui';
import FetchError from '../components/ui/FetchError';
import { LoadingSpinner } from '../components/LoadingSpinner';
import { AddDriverDialog } from '../components/settings/AddDriverDialog';
import { useDriverPackCatalog } from '../hooks/useDriverPacks';
import { usePageTitle } from '../hooks/usePageTitle';
import type { DriverPack, RuntimePolicy } from '../types/driverPacks';

const STATE_TONES: Record<string, 'success' | 'warning' | 'neutral'> = {
  enabled: 'success',
  draining: 'warning',
  disabled: 'neutral',
  draft: 'neutral',
};

function runtimePolicyLabel(policy: RuntimePolicy | undefined): string {
  if (!policy || policy.strategy === 'recommended') return 'recommended';
  if (policy.strategy === 'latest_patch') return 'latest patch';
  return `exact ${policy.appium_server_version}/${policy.appium_driver_version}`;
}

function versionList(versions: string[] | undefined): string {
  if (!versions || versions.length === 0) return 'none';
  if (versions.length <= 2) return versions.join(', ');
  return `${versions.slice(0, 2).join(', ')} +${versions.length - 2}`;
}

function VersionLines({ pack }: { pack: DriverPack }) {
  const summary = pack.runtime_summary;
  return (
    <div className="flex flex-col gap-1 text-xs">
      <span className="text-text-2">server rec {pack.appium_server?.recommended ?? 'none'}</span>
      <span className="text-text-3">server actual {versionList(summary?.actual_appium_server_versions)}</span>
      <span className="text-text-2">driver rec {pack.appium_driver?.recommended ?? 'none'}</span>
      <span className="text-text-3">driver actual {versionList(summary?.actual_appium_driver_versions)}</span>
      {(summary?.driver_drift_hosts ?? 0) > 0 && (
        <Badge tone="warning">
          {summary?.driver_drift_hosts} drift{summary?.driver_drift_hosts === 1 ? '' : 's'}
        </Badge>
      )}
    </div>
  );
}

const columns: DataTableColumn<DriverPack>[] = [
  {
    key: 'display_name',
    header: 'Name',
    render: (pack) => (
      <div className="flex flex-col gap-1">
        <Link to={`/drivers/${encodeURIComponent(pack.id)}`} className="font-medium text-accent hover:underline">
          {pack.display_name}
        </Link>
        <span className="text-xs text-text-3">{pack.id}</span>
      </div>
    ),
  },
  {
    key: 'current_release',
    header: 'Release',
    render: (pack) => <span className="text-text-2">{pack.current_release ?? 'No release'}</span>,
  },
  {
    key: 'platforms',
    header: 'Platforms',
    render: (pack) => <span className="text-text-2">{pack.platforms?.length ?? 0}</span>,
  },
  {
    key: 'runtime_policy',
    header: 'Runtime Policy',
    render: (pack) => <span className="text-text-2">{runtimePolicyLabel(pack.runtime_policy)}</span>,
  },
  {
    key: 'versions',
    header: 'Versions',
    render: (pack) => <VersionLines pack={pack} />,
  },
  {
    key: 'state',
    header: 'State',
    render: (pack) => (
      <div className="flex items-center gap-2">
        <Badge tone={STATE_TONES[pack.state] ?? 'neutral'}>{pack.state}</Badge>
        {pack.state === 'draining' && (
          <span className="text-xs text-text-3">
            {pack.active_runs} run{pack.active_runs !== 1 ? 's' : ''},{' '}
            {pack.live_sessions} session{pack.live_sessions !== 1 ? 's' : ''}
          </span>
        )}
      </div>
    ),
  },
];

export default function Drivers() {
  usePageTitle('Driver Packs');
  const { data, isLoading, error, refetch } = useDriverPackCatalog();
  const [uploadOpen, setUploadOpen] = useState(false);

  if (isLoading) return <LoadingSpinner />;
  if (error) return <FetchError message="Failed to load driver packs." onRetry={() => void refetch()} />;

  return (
    <div>
      <PageHeader
        title="Driver Packs"
        subtitle="Appium driver packs available to this fleet"
        actions={
          <Button size="sm" leadingIcon={<Package size={14} />} onClick={() => setUploadOpen(true)}>
            Upload Driver
          </Button>
        }
      />

      <DataTable
        columns={columns}
        rows={data ?? []}
        rowKey={(pack) => pack.id}
        caption="Driver packs"
        emptyState={<EmptyState icon={Package} title="No driver packs installed" />}
      />

      <AddDriverDialog isOpen={uploadOpen} onClose={() => setUploadOpen(false)} />
    </div>
  );
}
