import { useState } from 'react';
import { Play, Square, RefreshCw, Trash2, Wrench, Power, Wifi } from 'lucide-react';
import { toast } from 'sonner';
import {
  useGroupDeleteDevices,
  useGroupEnterMaintenance,
  useGroupExitMaintenance,
  useGroupReconnect,
  useGroupRestartNodes,
  useGroupStartNodes,
  useGroupStopNodes,
} from '../hooks/useDeviceGroups';
import { ConfirmDialog } from './ui/ConfirmDialog';
import { Card } from './ui/Card';
import type { BulkOperationResult, DeviceRead } from '../types';
import { DeviceActionErrorsDialog } from '../pages/devices/deviceActionDialogs';

interface Props {
  groupKey: string;
  devices: DeviceRead[];
}

interface PendingAction {
  title: string;
  message: string;
  action: () => Promise<void>;
}

function formatFailureLines(result: BulkOperationResult, devices: DeviceRead[]) {
  const nameById = new Map(devices.map((device) => [device.id, device.name]));
  return Object.entries(result.errors).map(([id, error]) => ({
    id,
    label: nameById.get(id) ?? id,
    error,
  }));
}

export function GroupActionBar({ groupKey, devices }: Props) {
  const [confirmAction, setConfirmAction] = useState<PendingAction | null>(null);
  const [showErrorsModal, setShowErrorsModal] = useState(false);
  const [lastResult, setLastResult] = useState<{ operation: string; result: BulkOperationResult } | null>(null);

  const startNodes = useGroupStartNodes();
  const stopNodes = useGroupStopNodes();
  const restartNodes = useGroupRestartNodes();
  const enterMaintenance = useGroupEnterMaintenance();
  const exitMaintenance = useGroupExitMaintenance();
  const reconnect = useGroupReconnect();
  const deleteDevices = useGroupDeleteDevices();

  const errorLines = lastResult ? formatFailureLines(lastResult.result, devices) : [];

  function confirm(title: string, message: string, action: () => Promise<void>) {
    setConfirmAction({ title, message, action });
  }

  async function runOperation(operation: string, fn: () => Promise<BulkOperationResult>) {
    try {
      const result = await fn();
      setLastResult({ operation, result });
      if (result.failed === 0) {
        toast.success(`${operation}: ${result.succeeded}/${result.total} succeeded`);
      } else {
        toast.warning(`${operation}: ${result.succeeded}/${result.total} succeeded, ${result.failed} failed`);
        setShowErrorsModal(true);
      }
    } catch {
      toast.error(`${operation} failed`);
    }
  }

  const btnClass = 'inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md transition-colors';
  const btnDefault = `${btnClass} text-text-2 bg-surface-1 border border-border-strong hover:bg-surface-2`;
  const btnDanger = `${btnClass} text-danger-foreground bg-surface-1 border border-danger-strong/30 hover:bg-danger-soft`;
  const count = devices.length;

  return (
    <>
      <Card padding="md" className="mb-6">
        <div className="flex items-center justify-between gap-4 mb-3">
          <div>
            <h2 className="text-sm font-semibold text-text-1">Whole Group Actions</h2>
            <p className="text-sm text-text-3">Run operations against all {count} current group devices.</p>
          </div>
          {lastResult?.result.failed ? (
            <button onClick={() => setShowErrorsModal(true)} className="text-sm text-warning-foreground hover:text-warning-foreground">
              View {lastResult.result.failed} error{lastResult.result.failed === 1 ? '' : 's'}
            </button>
          ) : null}
        </div>

        <div className="flex flex-wrap gap-2">
          <button onClick={() => confirm('Start Nodes', `Start nodes for all ${count} group devices?`, () => runOperation('Start Nodes', () => startNodes.mutateAsync(groupKey)))} className={btnDefault}>
            <Play size={14} /> Start
          </button>
          <button onClick={() => confirm('Stop Nodes', `Stop nodes for all ${count} group devices?`, () => runOperation('Stop Nodes', () => stopNodes.mutateAsync(groupKey)))} className={btnDefault}>
            <Square size={14} /> Stop
          </button>
          <button onClick={() => confirm('Restart Nodes', `Restart nodes for all ${count} group devices?`, () => runOperation('Restart Nodes', () => restartNodes.mutateAsync(groupKey)))} className={btnDefault}>
            <RefreshCw size={14} /> Restart
          </button>
          <button onClick={() => confirm('Reconnect', 'Reconnect supported devices in this group?', () => runOperation('Reconnect', () => reconnect.mutateAsync(groupKey)))} className={btnDefault}>
            <Wifi size={14} /> Reconnect
          </button>

          <button onClick={() => confirm('Enter Maintenance', `Put all ${count} group devices into maintenance mode?`, () => runOperation('Enter Maintenance', () => enterMaintenance.mutateAsync({ groupKey, body: { device_ids: [] } })))} className={btnDefault}>
            <Wrench size={14} /> Maintenance
          </button>
          <button onClick={() => confirm('Exit Maintenance', `Exit maintenance for all ${count} group devices?`, () => runOperation('Exit Maintenance', () => exitMaintenance.mutateAsync(groupKey)))} className={btnDefault}>
            <Power size={14} /> Exit Maint.
          </button>
          <button onClick={() => confirm('Delete Devices', `Delete all ${count} devices currently in this group? This cannot be undone.`, () => runOperation('Delete Devices', () => deleteDevices.mutateAsync(groupKey)))} className={btnDanger}>
            <Trash2 size={14} /> Delete Devices
          </button>
        </div>
      </Card>

      <ConfirmDialog
        isOpen={!!confirmAction}
        onClose={() => setConfirmAction(null)}
        onConfirm={async () => {
          if (confirmAction) {
            await confirmAction.action();
          }
        }}
        title={confirmAction?.title ?? ''}
        message={confirmAction?.message ?? ''}
        confirmLabel="Confirm"
        variant="danger"
      />



      <DeviceActionErrorsDialog
        isOpen={showErrorsModal}
        onClose={() => setShowErrorsModal(false)}
        title={lastResult ? `${lastResult.operation} Errors` : 'Operation Errors'}
        lines={errorLines}
      />
    </>
  );
}
