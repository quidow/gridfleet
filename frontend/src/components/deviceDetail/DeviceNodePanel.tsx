import { Play, Power, RefreshCw, Square, Wrench } from 'lucide-react';
import StatusBadge from '../../components/StatusBadge';
import { isEmulatorRunning, isEmulatorStopped } from '../../lib/emulatorState';
import {
  useEnterDeviceMaintenance,
  useExitDeviceMaintenance,
  useRestartNode,
  useRunDeviceLifecycleAction,
  useStartNode,
  useStopNode,
  useToggleDeviceAutoManage,
} from '../../hooks/useDevices';
import { platformDescriptorForDeviceType, usePlatformDescriptor } from '../../hooks/usePlatformDescriptor';
import {
  getPendingDeviceAction,
  getPendingDeviceActionLabel,
} from '../../lib/devicePendingAction';
import type { DeviceDetail } from '../../types';
import Button from '../ui/Button';
import DefinitionList from '../ui/DefinitionList';
import { formatDate } from './utils';

type Props = {
  device: DeviceDetail;
};

const LIFECYCLE_ACTION_LABELS: Record<string, string> = {
  boot: 'Boot',
  shutdown: 'Shutdown',
  reconnect: 'Reconnect',
  state: 'Refresh State',
};

function lifecycleActionLabel(action: string): string {
  return LIFECYCLE_ACTION_LABELS[action] ?? action.replaceAll('_', ' ').replace(/^\w/, (char) => char.toUpperCase());
}

export default function DeviceNodePanel({ device }: Props) {
  const startNode = useStartNode();
  const stopNode = useStopNode();
  const restartNode = useRestartNode();
  const enterMaintenance = useEnterDeviceMaintenance();
  const exitMaintenance = useExitDeviceMaintenance();
  const toggleAutoManage = useToggleDeviceAutoManage();
  const lifecycleAction = useRunDeviceLifecycleAction();
  const baseDescriptor = usePlatformDescriptor(device.pack_id, device.platform_id);
  const descriptor = platformDescriptorForDeviceType(baseDescriptor, device.device_type);
  const lifecycleActions = descriptor?.lifecycleActions ?? [];
  const node = device.appium_node;
  const reservation = device.reservation;
  const reservationLocked = !!reservation;
  const maintenanceLocked = device.availability_status === 'maintenance';
  const readinessLocked = device.readiness_state !== 'verified';
  const pendingAction = getPendingDeviceAction(device.id, [
    {
      action: 'updating-auto-manage',
      isPending: toggleAutoManage.isPending,
      deviceId: toggleAutoManage.variables?.id,
    },
    {
      action: 'entering-maintenance',
      isPending: enterMaintenance.isPending,
      deviceId: enterMaintenance.variables?.id,
    },
    {
      action: 'exiting-maintenance',
      isPending: exitMaintenance.isPending,
      deviceId: exitMaintenance.variables,
    },
    {
      action: 'starting',
      isPending: startNode.isPending,
      deviceId: startNode.variables,
    },
    {
      action: 'stopping',
      isPending: stopNode.isPending,
      deviceId: stopNode.variables,
    },
    {
      action: 'restarting',
      isPending: restartNode.isPending,
      deviceId: restartNode.variables,
    },
    {
      action: 'running-lifecycle-action',
      isPending: lifecycleAction.isPending,
      deviceId: lifecycleAction.variables?.id,
    },
  ]);
  const rowBusy = pendingAction !== null;
  const pendingLabel = getPendingDeviceActionLabel(pendingAction);

  return (
    <div>
      <div className="mb-4">
        <h2 className="text-sm font-semibold text-text-1">Device Control</h2>
        <p className="mt-1 text-xs text-text-2">Device registration, auto-management, and pack lifecycle controls.</p>
      </div>
      {node ? (
        <>
          <div className="mb-3 flex items-center justify-between gap-3 rounded-md border border-border bg-surface-2/60 px-3 py-2">
            <div>
              <h3 className="text-xs font-medium uppercase tracking-wide text-text-3">Appium Node</h3>
              <p className="mt-0.5 text-sm font-medium text-text-1">{node.grid_url}</p>
            </div>
            <StatusBadge status={node.state} />
          </div>
          <DefinitionList
            className="mb-4"
            items={[
              ['Port', node.port],
              ['Active Connection Target', node.active_connection_target ?? '-'],
              ['PID', node.pid ?? '-'],
              ['Started', formatDate(node.started_at)],
            ].map(([term, value]) => ({
              term,
              definition: <span className="block max-w-[min(28rem,55vw)] truncate font-medium">{String(value)}</span>,
            }))}
          />
          <div className="mb-3 flex items-center justify-between gap-3 rounded-md border border-border bg-surface-1 px-3 py-2">
            <label className="flex cursor-pointer select-none items-center gap-2 text-sm text-text-2">
              <input
                type="checkbox"
                checked={device.auto_manage}
                onChange={(event) => toggleAutoManage.mutate({ id: device.id, autoManage: event.target.checked })}
                disabled={rowBusy}
                className="h-4 w-4 rounded border-border-strong text-accent focus:ring-accent"
              />
              Auto-manage
            </label>
            <span className="text-xs text-text-3">
              {pendingLabel ?? (device.auto_manage ? 'Node will auto-restart on failure' : 'Manual control only')}
            </span>
          </div>
          <div className="mb-3 flex flex-wrap gap-2">
            {maintenanceLocked ? (
              <Button
                size="sm"
                variant="secondary"
                onClick={() => exitMaintenance.mutate(device.id)}
                disabled={rowBusy}
                leadingIcon={<Power size={14} />}
              >
                {pendingAction === 'exiting-maintenance' ? 'Exiting...' : 'Exit Maintenance'}
              </Button>
            ) : (
              <Button
                size="sm"
                variant="secondary"
                onClick={() => enterMaintenance.mutate({ id: device.id, drain: false })}
                disabled={rowBusy}
                leadingIcon={<Wrench size={14} />}
              >
                {pendingAction === 'entering-maintenance' ? 'Entering...' : 'Enter Maintenance'}
              </Button>
            )}
          </div>
          <div className="flex flex-wrap gap-2">
            {node.state === 'running' ? (
              <>
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => stopNode.mutate(device.id)}
                  disabled={rowBusy || reservationLocked}
                  leadingIcon={<Square size={14} />}
                  title={reservation ? `Reserved by ${reservation.run_name}` : 'Stop node'}
                >
                  {pendingAction === 'stopping' ? 'Stopping...' : 'Stop'}
                </Button>
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => restartNode.mutate(device.id)}
                  disabled={rowBusy || reservationLocked || maintenanceLocked || readinessLocked}
                  leadingIcon={<RefreshCw size={14} />}
                  title={
                    reservation
                      ? `Reserved by ${reservation.run_name}`
                      : maintenanceLocked
                        ? 'Disabled during maintenance'
                        : readinessLocked
                          ? 'Complete setup and verification first'
                          : 'Restart node'
                  }
                >
                  {pendingAction === 'restarting' ? 'Restarting...' : 'Restart'}
                </Button>
              </>
            ) : (
              <Button
                size="sm"
                onClick={() => startNode.mutate(device.id)}
                disabled={rowBusy || reservationLocked || maintenanceLocked || readinessLocked}
                leadingIcon={<Play size={14} />}
                title={
                  reservation
                    ? `Reserved by ${reservation.run_name}`
                    : maintenanceLocked
                      ? 'Disabled during maintenance'
                      : readinessLocked
                        ? 'Complete setup and verification first'
                        : 'Start node'
                }
              >
                {pendingAction === 'starting' ? 'Starting...' : 'Start'}
              </Button>
            )}
          </div>
        </>
      ) : (
        <div>
          <div className="mb-3 flex items-center justify-between gap-3 rounded-md border border-border bg-surface-1 px-3 py-2">
            <label className="flex cursor-pointer select-none items-center gap-2 text-sm text-text-2">
              <input
                type="checkbox"
                checked={device.auto_manage}
                onChange={(event) => toggleAutoManage.mutate({ id: device.id, autoManage: event.target.checked })}
                disabled={rowBusy}
                className="h-4 w-4 rounded border-border-strong text-accent focus:ring-accent"
              />
              Auto-manage
            </label>
            <span className="text-xs text-text-3">
              {pendingLabel ?? (device.auto_manage ? 'Node will auto-restart on failure' : 'Manual control only')}
            </span>
          </div>
          <Button
            size="sm"
            onClick={() => startNode.mutate(device.id)}
            disabled={rowBusy || reservationLocked || maintenanceLocked || readinessLocked}
            leadingIcon={<Play size={14} />}
            title={
              reservation
                ? `Reserved by ${reservation.run_name}`
                : maintenanceLocked
                  ? 'Disabled during maintenance'
                  : readinessLocked
                    ? 'Complete setup and verification first'
                    : 'Start node'
            }
          >
            {pendingAction === 'starting' ? 'Starting...' : 'Start Node'}
          </Button>
        </div>
      )}
      {lifecycleActions.length > 0 && (
        <div className="mt-4 border-t border-border pt-4">
          <h3 className="mb-3 text-xs font-medium uppercase tracking-wide text-text-3">Lifecycle</h3>
          <div className="flex flex-wrap gap-2">
            {lifecycleActions.map((action) => {
              const isBoot = action === 'boot';
              const isShutdown = action === 'shutdown';
              const disabled =
                rowBusy ||
                (isBoot && isEmulatorRunning(device.emulator_state)) ||
                (isShutdown && isEmulatorStopped(device.emulator_state));
              return (
                <Button
                  key={action}
                  size="sm"
                  variant={isShutdown ? 'secondary' : 'primary'}
                  onClick={() => lifecycleAction.mutate({ id: device.id, action })}
                  disabled={disabled}
                  leadingIcon={isShutdown ? <Square size={14} /> : <Play size={14} />}
                  title={lifecycleActionLabel(action)}
                >
                  {pendingAction === 'running-lifecycle-action' && lifecycleAction.variables?.action === action
                    ? `${lifecycleActionLabel(action)}...`
                    : lifecycleActionLabel(action)}
                </Button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
