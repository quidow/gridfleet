import { Link } from 'react-router-dom';
import { Server } from 'lucide-react';
import LoadingSpinner from '../../components/LoadingSpinner';
import FetchError from '../../components/ui/FetchError';
import { Badge, DataTable, EmptyState, type DataTableColumn } from '../../components/ui';
import { useDriverPackHosts } from '../../hooks/useDriverDetail';
import type { DriverPackHostStatus } from '../../types/driverPacks';

const STATUS_TONE: Record<string, 'success' | 'warning' | 'danger' | 'neutral'> = {
  online: 'success',
  installed: 'success',
  blocked: 'danger',
  offline: 'neutral',
  pending: 'warning',
};

function StatusBadge({ value }: { value: string | null }) {
  if (!value) return <span className="text-text-3">None</span>;
  return <Badge tone={STATUS_TONE[value] ?? 'neutral'}>{value}</Badge>;
}

function desiredAppiumServer(row: DriverPackHostStatus): string | null {
  const value = row.resolved_install_spec?.appium_server;
  return typeof value === 'string' && value ? value : null;
}

const columns: DataTableColumn<DriverPackHostStatus>[] = [
  {
    key: 'host',
    header: 'Host',
    render: (row) => (
      <div className="flex flex-col gap-1">
        <Link to={`/hosts/${row.host_id}`} className="font-medium text-accent hover:underline">
          {row.hostname}
        </Link>
        <StatusBadge value={row.status} />
      </div>
    ),
  },
  {
    key: 'pack',
    header: 'Pack',
    render: (row) => (
      <div className="flex flex-col gap-1">
        <span className="font-mono text-sm text-text-2">{row.pack_release}</span>
        <StatusBadge value={row.pack_status} />
      </div>
    ),
  },
  {
    key: 'runtime',
    header: 'Actual Runtime',
    render: (row) => {
      const desiredServer = desiredAppiumServer(row);
      return (
        <div className="flex flex-col gap-1">
          <span className="font-mono text-sm text-text-2">{row.runtime_id ?? 'None'}</span>
          <span className="text-xs text-text-3">actual appium@{row.appium_server_version ?? 'unknown'}</span>
          {desiredServer && <span className="text-xs text-text-3">desired {desiredServer}</span>}
        </div>
      );
    },
  },
  {
    key: 'driver',
    header: 'Driver',
    render: (row) => (
      <div className="flex flex-col gap-1">
        <span className="font-mono text-sm text-text-2">{row.installed_appium_driver_version ?? 'not installed'}</span>
        {row.appium_driver_drift && row.desired_appium_driver_version && (
          <span className="text-xs text-warning-foreground">wanted: {row.desired_appium_driver_version}</span>
        )}
      </div>
    ),
  },
  {
    key: 'doctor',
    header: 'Doctor',
    render: (row) => {
      if (row.doctor.length === 0) return <span className="text-sm text-text-3">None</span>;
      const failing = row.doctor.filter((check) => !check.ok);
      return (
        <div className="flex flex-wrap gap-1">
          {row.doctor.map((check) => (
            <Badge key={check.check_id} tone={check.ok ? 'success' : 'danger'} title={check.message}>
              {check.check_id}
            </Badge>
          ))}
          {failing.length > 0 && <span className="sr-only">{failing.length} failing checks</span>}
        </div>
      );
    },
  },
];

export default function DriverHostsPanel({ packId }: { packId: string }) {
  const { data, isLoading, error, refetch } = useDriverPackHosts(packId);

  if (isLoading) return <LoadingSpinner />;

  return (
    <DataTable
      columns={columns}
      rows={data?.hosts ?? []}
      rowKey={(row) => row.host_id}
      error={
        error ? (
          <FetchError message="Driver pack host status could not be loaded." onRetry={() => void refetch()} />
        ) : undefined
      }
      emptyState={<EmptyState icon={Server} title="No host installations" />}
      caption="Driver pack host installations"
    />
  );
}
