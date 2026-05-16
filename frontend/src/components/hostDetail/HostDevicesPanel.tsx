import { Link } from 'react-router-dom';
import { PlatformIcon } from '../PlatformIcon';
import { AvailabilityCell } from '../../pages/devices/deviceColumns';
import { DataTable } from '../ui';
import type { DataTableColumn } from '../ui';
import type { DeviceRead, HostDetail } from '../../types';

const HOST_DEVICE_COLUMNS: DataTableColumn<DeviceRead>[] = [
  {
    key: 'name',
    header: 'Name',
    render: (device) => (
      <Link to={`/devices/${device.id}`} className="font-medium text-accent hover:underline text-sm">
        {device.name}
      </Link>
    ),
  },
  {
    key: 'identity',
    header: 'Identity',
    render: (device) => (
      <span className="font-mono text-sm text-text-3">
        {device.identity_value.length > 16 ? `${device.identity_value.slice(0, 16)}...` : device.identity_value}
      </span>
    ),
  },
  {
    key: 'platform',
    header: 'Platform',
    render: (device) => <PlatformIcon platformId={device.platform_id} platformLabel={device.platform_label} />,
  },
  {
    key: 'os_version',
    header: 'OS',
    render: (device) => <span className="text-sm text-text-2">{device.os_version}</span>,
  },
  {
    key: 'status',
    header: 'Status',
    render: (device) => <AvailabilityCell device={device} />,
  },
];

type Props = {
  host: HostDetail;
};

export default function HostDevicesPanel({ host }: Props) {
  return (
    <div className="rounded-lg border border-border bg-surface-1">
      <div className="border-b border-border px-5 py-4">
        <h2 className="text-sm font-medium text-text-2">Devices ({host.devices.length})</h2>
      </div>
      <DataTable<DeviceRead>
        columns={HOST_DEVICE_COLUMNS}
        rows={host.devices}
        rowKey={(d) => d.id}
        emptyState={<p className="px-5 py-8 text-center text-sm text-text-3">No devices on this host.</p>}
      />
    </div>
  );
}
