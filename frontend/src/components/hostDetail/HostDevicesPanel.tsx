import { Search } from 'lucide-react';
import { Link } from 'react-router-dom';
import { PlatformIcon } from '../PlatformIcon';
import { AvailabilityCell } from '../../pages/devices/deviceColumns';
import { Badge, type BadgeTone } from '../ui/Badge';
import { Button } from '../ui/Button';
import { DataTable } from '../ui';
import type { DataTableColumn } from '../ui';
import { Card } from '../ui/Card';
import { useHostDiagnostics } from '../../hooks/useHosts';
import type { DeviceRead, HostDetail, HostDiagnosticsNode } from '../../types';

type DeviceRow =
  | { kind: 'device'; device: DeviceRead; node: HostDiagnosticsNode | null }
  | { kind: 'unmapped'; node: HostDiagnosticsNode };

function getNode(row: DeviceRow): HostDiagnosticsNode | null {
  return row.kind === 'device' ? row.node : row.node;
}

function appiumStateTone(nodeState: string | null, managed: boolean): BadgeTone {
  if (!managed) return 'neutral';
  if (nodeState === 'running') return 'success';
  if (nodeState === 'error') return 'critical';
  if (nodeState === 'stopped') return 'neutral';
  return 'info';
}

function appiumStateLabel(nodeState: string | null, managed: boolean): string {
  if (!managed) return 'Unmapped';
  if (nodeState === 'running') return 'Running';
  if (nodeState === 'error') return 'Error';
  if (nodeState === 'stopped') return 'Stopped';
  return 'Managed';
}

const HOST_DEVICE_COLUMNS: DataTableColumn<DeviceRow>[] = [
  {
    key: 'name',
    header: 'Name',
    render: (row) => {
      if (row.kind === 'unmapped') {
        return <span className="text-sm text-text-3 italic">Unmapped process</span>;
      }
      return (
        <Link to={`/devices/${row.device.id}`} className="font-medium text-accent hover:underline text-sm">
          {row.device.name}
        </Link>
      );
    },
  },
  {
    key: 'identity',
    header: 'Identity',
    render: (row) => {
      if (row.kind === 'unmapped') return <span className="text-sm text-text-3">—</span>;
      const val = row.device.identity_value;
      return (
        <span className="font-mono text-sm text-text-3">
          {val.length > 16 ? `${val.slice(0, 16)}...` : val}
        </span>
      );
    },
  },
  {
    key: 'platform',
    header: 'Platform',
    render: (row) => {
      if (row.kind === 'unmapped') {
        return row.node.platform_id
          ? <PlatformIcon platformId={row.node.platform_id} platformLabel={null} />
          : <span className="text-sm text-text-3">—</span>;
      }
      return <PlatformIcon platformId={row.device.platform_id} platformLabel={row.device.platform_label} />;
    },
  },
  {
    key: 'os_version',
    header: 'OS',
    render: (row) => {
      if (row.kind === 'unmapped') return <span className="text-sm text-text-3">—</span>;
      return <span className="text-sm text-text-2">{row.device.os_version}</span>;
    },
  },
  {
    key: 'status',
    header: 'Status',
    render: (row) => {
      if (row.kind === 'unmapped') return <span className="text-sm text-text-3">—</span>;
      return <AvailabilityCell device={row.device} />;
    },
  },
  {
    key: 'appium',
    header: 'Appium',
    render: (row) => {
      const node = getNode(row);
      if (!node) return <span className="text-sm text-text-3">—</span>;
      return (
        <Badge tone={appiumStateTone(node.node_state, node.managed)}>
          {appiumStateLabel(node.node_state, node.managed)}
        </Badge>
      );
    },
  },
  {
    key: 'port',
    header: 'Port',
    render: (row) => {
      const node = getNode(row);
      if (!node) return <span className="text-sm text-text-3">—</span>;
      return <span className="text-sm text-text-2">{node.port}</span>;
    },
  },
  {
    key: 'pid',
    header: 'PID',
    render: (row) => {
      const node = getNode(row);
      if (!node) return <span className="text-sm text-text-3">—</span>;
      return <span className="text-sm text-text-2">{node.pid ?? '—'}</span>;
    },
  },
];

function buildRows(devices: DeviceRead[], nodes: HostDiagnosticsNode[]): DeviceRow[] {
  const nodeByDeviceId = new Map<string, HostDiagnosticsNode>();
  const unmappedNodes: HostDiagnosticsNode[] = [];

  for (const node of nodes) {
    if (node.device_id) {
      nodeByDeviceId.set(node.device_id, node);
    } else {
      unmappedNodes.push(node);
    }
  }

  const deviceRows: DeviceRow[] = devices.map((device) => ({
    kind: 'device' as const,
    device,
    node: nodeByDeviceId.get(device.id) ?? null,
  }));

  const unmappedRows: DeviceRow[] = unmappedNodes.map((node) => ({
    kind: 'unmapped' as const,
    node,
  }));

  return [...deviceRows, ...unmappedRows];
}

type Props = {
  host: HostDetail;
  onDiscover: () => void;
  discoverPending: boolean;
};

export function HostDevicesPanel({ host, onDiscover, discoverPending }: Props) {
  const { data: diagnostics } = useHostDiagnostics(host.id);
  const nodes = diagnostics?.appium_processes.running_nodes ?? [];
  const rows = buildRows(host.devices, nodes);
  const totalCount = rows.length;

  return (
    <Card padding="none">
      <div className="flex items-center justify-between border-b border-border px-5 py-4">
        <h2 className="text-sm font-medium text-text-2">Devices ({totalCount})</h2>
        <Button
          variant="secondary"
          size="sm"
          leadingIcon={<Search size={14} />}
          onClick={onDiscover}
          loading={discoverPending}
        >
          Discover Devices
        </Button>
      </div>
      <DataTable<DeviceRow>
        columns={HOST_DEVICE_COLUMNS}
        rows={rows}
        rowKey={(r) => r.kind === 'device' ? r.device.id : `unmapped-${r.node.port}-${r.node.connection_target ?? 'process'}`}
        emptyState={<p className="px-5 py-8 text-center text-sm text-text-3">No devices on this host.</p>}
      />
    </Card>
  );
}
