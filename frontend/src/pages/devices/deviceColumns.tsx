/* eslint-disable react-refresh/only-export-components -- intentional mixed module: exports component + builder functions */
import { Link } from 'react-router-dom';
import { AlertTriangle, Cable, Cloud, LockKeyhole, Pencil, Play, Power, RefreshCw, Square, Trash2, Wifi, Wrench } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import PlatformIcon from '../../components/PlatformIcon';
import Badge, { type BadgeTone } from '../../components/ui/Badge';
import Popover from '../../components/ui/Popover';
import { missingSetupFieldLabel } from '../../components/readiness';
import { deviceChipStatus } from '../../lib/deviceState';
import { isEmulatorStopped } from '../../lib/emulatorState';
import { DEVICE_STATUS_LABELS } from '../../lib/labels';
import { getPendingDeviceActionLabel, type DevicePendingAction } from '../../lib/devicePendingAction';
import type { RowActionItem } from '../../components/RowActionsMenu';
import type { DataTableColumn } from '../../components/ui/DataTable';
import type { DeviceChipStatus, DeviceRead } from '../../types';
import { CONNECTION_TYPE_LABELS, DEVICE_TYPE_COLORS, DEVICE_TYPE_LABELS } from './devicePageHelpers';
import type { DeviceSortKey } from './devicePageHelpers';
import DeviceHealthCell from './DeviceHealthCell';
import type { DeviceAction } from './deviceActions';

function availabilityTone(status: DeviceChipStatus): BadgeTone {
  switch (status) {
    case 'available': return 'success';
    case 'busy': return 'warning';
    case 'verifying': return 'warning';
    case 'offline': return 'critical';
    case 'maintenance': return 'neutral';
    case 'reserved': return 'info';
  }
}

export function AvailabilityCell({ device }: { device: DeviceRead }) {
  const status = deviceChipStatus(device);
  return (
    <Badge tone={availabilityTone(status)}>
      {DEVICE_STATUS_LABELS[status]}
    </Badge>
  );
}

function PendingDot() {
  return (
    <span
      aria-hidden="true"
      className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-accent"
    />
  );
}

function StateCell({ device, pendingAction }: { device: DeviceRead; pendingAction: DevicePendingAction | null }) {
  const pendingLabel = getPendingDeviceActionLabel(pendingAction);
  const reservation = device.reservation;
  const exclusionReason = reservation?.excluded ? reservation.exclusion_reason : null;
  const cooldownRemaining = reservation?.cooldown_remaining_sec ?? null;
  const cooldownActive = cooldownRemaining !== null && cooldownRemaining > 0;
  const missingSetup = device.missing_setup_fields;
  const hasWarning = !!exclusionReason || cooldownActive || missingSetup.length > 0;
  const hasDetail = !!(pendingLabel || reservation || exclusionReason || missingSetup.length > 0);

  const trigger = (
    <span className="inline-flex items-center gap-1.5">
      <AvailabilityCell device={device} />
      {pendingLabel ? <PendingDot /> : null}
      {reservation ? <LockKeyhole size={12} aria-hidden="true" className="shrink-0 text-accent" /> : null}
      {hasWarning ? <AlertTriangle size={12} aria-hidden="true" className="shrink-0 text-warning-strong" /> : null}
    </span>
  );

  if (!hasDetail) return trigger;

  return (
    <Popover
      ariaLabel={`State details for ${device.name}`}
      trigger={trigger}
      triggerClassName="inline-flex items-center gap-1.5 rounded px-0.5 hover:bg-surface-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent-ring"
    >
      <div className="space-y-2 text-xs leading-snug">
        {pendingLabel ? <p className="italic text-accent">{pendingLabel}</p> : null}
        {reservation ? (
          <div>
            <p className="heading-label mb-0.5">Reserved by</p>
            <Link
              to={`/runs/${reservation.run_id}`}
              className="inline-flex items-center gap-1 text-accent transition-colors hover:text-accent-hover hover:underline"
            >
              <LockKeyhole size={12} />
              <span>
                {reservation.run_name}
                {reservation.excluded ? ' (excluded)' : ''}
              </span>
            </Link>
          </div>
        ) : null}
        {exclusionReason ? (
          <div>
            <p className="heading-label mb-0.5">Exclusion reason</p>
            <p className="text-warning-foreground">{exclusionReason}</p>
          </div>
        ) : null}
        {cooldownActive ? (
          <div>
            <p className="heading-label mb-0.5">Cooldown</p>
            <p className="text-warning-foreground">
              {cooldownRemaining}s left
              {reservation?.excluded_until ? ` until ${new Date(reservation.excluded_until).toLocaleTimeString()}` : ''}
            </p>
          </div>
        ) : null}
        {missingSetup.length > 0 ? (
          <div>
            <p className="heading-label mb-0.5">Missing setup</p>
            <p className="text-warning-foreground">{missingSetup.map((f) => missingSetupFieldLabel(f)).join(', ')}</p>
          </div>
        ) : null}
      </div>
    </Popover>
  );
}

function AutoManageToggle({
  device,
  rowBusy,
  onAction,
}: {
  device: DeviceRead;
  rowBusy: boolean;
  onAction: (action: DeviceAction) => void;
}) {
  return (
    <label
      className="relative inline-flex h-5 w-9 cursor-pointer items-center align-middle"
      title={device.auto_manage ? 'Auto-manage enabled' : 'Auto-manage disabled'}
    >
      <input
        type="checkbox"
        checked={device.auto_manage}
        disabled={rowBusy}
        onChange={(event) =>
          onAction({ type: 'toggle-auto-manage', deviceId: device.id, autoManage: event.target.checked })
        }
        className="peer absolute inset-0 z-10 h-full w-full cursor-pointer appearance-none rounded-full opacity-0 disabled:cursor-not-allowed"
        aria-label={`Toggle auto-manage for ${device.name}`}
      />
      <span className="pointer-events-none absolute inset-0 rounded-full bg-border transition peer-checked:bg-accent peer-focus-visible:ring-2 peer-focus-visible:ring-accent peer-focus-visible:ring-offset-2 peer-focus-visible:ring-offset-surface-1 peer-disabled:opacity-50" />
      <span className="pointer-events-none relative ml-0.5 h-4 w-4 rounded-full bg-surface-1 shadow-sm transition peer-checked:translate-x-4 peer-disabled:opacity-70" />
    </label>
  );
}

export function buildDeviceMenuItems(
  device: DeviceRead,
  pendingAction: DevicePendingAction | null,
  onAction: (action: DeviceAction) => void,
): RowActionItem[] {
  const rowBusy = pendingAction !== null;
  return [
    device.hold === 'maintenance'
      ? {
          key: 'exit-maintenance',
          label: pendingAction === 'exiting-maintenance' ? 'Exiting Maintenance...' : 'Exit Maintenance',
          icon: <Power size={15} />,
          onSelect: () => onAction({ type: 'exit-maintenance', deviceId: device.id }),
          disabled: rowBusy,
        }
      : {
          key: 'enter-maintenance',
          label: pendingAction === 'entering-maintenance' ? 'Entering Maintenance...' : 'Enter Maintenance',
          icon: <Wrench size={15} />,
          onSelect: () => onAction({ type: 'enter-maintenance', deviceId: device.id }),
          disabled: rowBusy,
        },
    {
      key: 'start-node',
      label: pendingAction === 'starting' ? 'Starting Node...' : 'Start Node',
      icon: <Play size={15} />,
      onSelect: () => onAction({ type: 'start-node', deviceId: device.id }),
      disabled:
        rowBusy || !!device.reservation || device.hold === 'maintenance' ||
        device.readiness_state !== 'verified' || isEmulatorStopped(device.emulator_state),
      title: isEmulatorStopped(device.emulator_state)
        ? 'Emulator/simulator is not running'
        : device.reservation
          ? `Reserved by ${device.reservation.run_name}`
          : device.hold === 'maintenance'
            ? 'Disabled during maintenance'
            : device.readiness_state !== 'verified'
              ? 'Complete setup and verification first'
              : 'Start Node',
    },
    {
      key: 'stop-node',
      label: pendingAction === 'stopping' ? 'Stopping Node...' : 'Stop Node',
      icon: <Square size={15} />,
      onSelect: () => onAction({ type: 'stop-node', deviceId: device.id }),
      disabled: rowBusy || !!device.reservation || isEmulatorStopped(device.emulator_state),
      title: isEmulatorStopped(device.emulator_state)
        ? 'Emulator/simulator is not running'
        : device.reservation
          ? `Reserved by ${device.reservation.run_name}`
          : 'Stop Node',
    },
    {
      key: 'restart-node',
      label: pendingAction === 'restarting' ? 'Restarting Node...' : 'Restart Node',
      icon: <RefreshCw size={15} />,
      onSelect: () => onAction({ type: 'restart-node', deviceId: device.id }),
      disabled:
        rowBusy || !!device.reservation || device.hold === 'maintenance' ||
        device.readiness_state !== 'verified' || isEmulatorStopped(device.emulator_state),
      title: isEmulatorStopped(device.emulator_state)
        ? 'Emulator/simulator is not running'
        : device.reservation
          ? `Reserved by ${device.reservation.run_name}`
          : device.hold === 'maintenance'
            ? 'Disabled during maintenance'
            : device.readiness_state !== 'verified'
              ? 'Complete setup and verification first'
              : 'Restart Node',
    },
    {
      key: 'verify',
      label:
        device.readiness_state === 'setup_required'
          ? 'Complete Setup'
          : device.readiness_state === 'verified'
            ? 'Re-verify Device'
            : 'Verify Device',
      icon: <LockKeyhole size={15} />,
      onSelect: () => onAction({ type: 'verify', device }),
      disabled: rowBusy,
    },
    {
      key: 'edit',
      label: 'Edit Configuration',
      icon: <Pencil size={15} />,
      onSelect: () => onAction({ type: 'edit', device }),
      disabled: rowBusy,
    },
    {
      key: 'delete',
      label: 'Delete Device',
      icon: <Trash2 size={15} />,
      onSelect: () => onAction({ type: 'delete', deviceId: device.id }),
      tone: 'critical',
      disabled: rowBusy,
    },
  ];
}

export type DeviceColumnContext = {
  hostMap: Map<string, string>;
  pendingActionForDevice: (id: string) => DevicePendingAction | null;
  onAction: (action: DeviceAction) => void;
};

export function buildDeviceColumns(ctx: DeviceColumnContext): DataTableColumn<DeviceRead, DeviceSortKey>[] {
  return [
    {
      key: 'name',
      header: 'Device',
      sortKey: 'name',
      render: (device) => (
        <Link
          to={`/devices/${device.id}`}
          className="block truncate font-semibold text-accent transition-colors hover:text-accent-hover hover:underline"
          title={device.name}
        >
          {device.name}
        </Link>
      ),
    },
    {
      key: 'platform',
      header: 'Platform',
      sortKey: 'platform',
      width: '9.5rem',
      className: 'whitespace-nowrap',
      render: (device) => <PlatformIcon platformId={device.platform_id} platformLabel={device.platform_label} />,
    },
    {
      key: 'os_version_display',
      header: 'OS',
      sortKey: 'os_version_display',
      width: '5rem',
      className: 'devices-table-optional-narrow font-mono tabular-nums text-text-2 whitespace-nowrap',
      headerClassName: 'devices-table-optional-narrow',
      render: (device) => device.os_version_display ?? device.os_version,
    },
    {
      key: 'device_type',
      header: 'Type',
      sortKey: 'device_type',
      width: '6rem',
      className: 'whitespace-nowrap',
      render: (device) =>
        device.device_type ? (
          <span
            className={`inline-flex items-center rounded px-2 py-0.5 text-xs font-medium ${DEVICE_TYPE_COLORS[device.device_type]}`}
          >
            {DEVICE_TYPE_LABELS[device.device_type]}
          </span>
        ) : (
          <span className="text-text-3">—</span>
        ),
    },
    {
      key: 'connection_type',
      header: 'Connection',
      sortKey: 'connection_type',
      width: '6rem',
      className: 'devices-table-optional-connection whitespace-nowrap',
      headerClassName: 'devices-table-optional-connection',
      render: (device) => {
        if (!device.connection_type) {
          return <span className="text-text-3">—</span>;
        }
        const Icon: LucideIcon =
          device.connection_type === 'usb'
            ? Cable
            : device.connection_type === 'network'
              ? Wifi
              : Cloud;
        return (
          <span className="inline-flex items-center gap-1.5 text-text-2">
            <Icon size={14} aria-hidden="true" className="shrink-0 text-text-3" />
            {CONNECTION_TYPE_LABELS[device.connection_type]}
          </span>
        );
      },
    },
    {
      key: 'host',
      header: 'Host',
      sortKey: 'host',
      width: '9rem',
      render: (device) => {
        const hostName = ctx.hostMap.get(device.host_id);
        return (
          <Link
            to={`/hosts/${device.host_id}`}
            className="block truncate text-accent transition-colors hover:text-accent-hover hover:underline"
            title={hostName ?? device.host_id}
          >
            {hostName ?? device.host_id}
          </Link>
        );
      },
    },
    {
      key: 'status',
      header: 'Availability',
      sortKey: 'status',
      width: '9rem',
      className: 'whitespace-nowrap',
      render: (device) => (
        <StateCell device={device} pendingAction={ctx.pendingActionForDevice(device.id)} />
      ),
    },
    {
      key: 'health',
      header: 'Health',
      width: '8rem',
      className: 'whitespace-nowrap',
      render: (device) => <DeviceHealthCell device={device} />,
    },
    {
      key: 'auto_manage',
      header: 'Auto',
      align: 'center',
      width: '4.5rem',
      render: (device) => (
        <AutoManageToggle
          device={device}
          rowBusy={ctx.pendingActionForDevice(device.id) !== null}
          onAction={ctx.onAction}
        />
      ),
    },
  ];
}
