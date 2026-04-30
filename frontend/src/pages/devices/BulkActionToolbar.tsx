import { useEffect, useState } from 'react';
import { Play, Square, RefreshCw, Trash2, Wrench, X, Power, Wifi, Tags } from 'lucide-react';
import { toast } from 'sonner';
import {
  useBulkStartNodes,
  useBulkStopNodes,
  useBulkRestartNodes,
  useBulkSetAutoManage,
  useBulkDelete,
  useBulkEnterMaintenance,
  useBulkExitMaintenance,
  useBulkReconnect,
  useBulkUpdateTags,
} from '../../hooks/useBulk';
import { Button } from '../../components/ui';
import ConfirmDialog from '../../components/ui/ConfirmDialog';
import Popover from '../../components/ui/Popover';
import type { BulkOperationResult, DeviceRead } from '../../types';
import {
  DeviceActionErrorsDialog,
  TagsActionDialog,
} from './deviceActionDialogs';
import { parseDeviceActionTags } from './deviceActionUtils';

interface Props {
  selectedIds: Set<string>;
  selectedDevices: DeviceRead[];
  onClearSelection: () => void;
}

function Divider() {
  return <div className="h-6 w-px bg-sidebar-border" aria-hidden="true" />;
}

export default function BulkActionToolbar({ selectedIds, selectedDevices, onClearSelection }: Props) {
  const [confirmAction, setConfirmAction] = useState<{ title: string; message: string; action: () => Promise<void> } | null>(null);
  const [showAutoManageMenu, setShowAutoManageMenu] = useState(false);
  const [showTagsModal, setShowTagsModal] = useState(false);
  const [showErrorsModal, setShowErrorsModal] = useState(false);
  const [tagsText, setTagsText] = useState('{\n  "team": "qa"\n}');
  const [mergeTags, setMergeTags] = useState(true);
  const [tagsError, setTagsError] = useState<string | null>(null);
  const [lastResult, setLastResult] = useState<{ operation: string; result: BulkOperationResult } | null>(null);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== 'Escape') return;
      if (showAutoManageMenu) {
        setShowAutoManageMenu(false);
        return;
      }
      if (confirmAction || showTagsModal || showErrorsModal) return;
      onClearSelection();
    }

    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [onClearSelection, showAutoManageMenu, confirmAction, showTagsModal, showErrorsModal]);

  const startNodes = useBulkStartNodes();
  const stopNodes = useBulkStopNodes();
  const restartNodes = useBulkRestartNodes();
  const setAutoManage = useBulkSetAutoManage();
  const updateTags = useBulkUpdateTags();
  const deleteMut = useBulkDelete();
  const enterMaintenance = useBulkEnterMaintenance();
  const exitMaintenance = useBulkExitMaintenance();
  const reconnectMut = useBulkReconnect();

  const ids = Array.from(selectedIds);
  const count = ids.length;
  const nameById = new Map(selectedDevices.map((device) => [device.id, device.name]));

  function confirm(title: string, message: string, action: () => Promise<void>) {
    setConfirmAction({ title, message, action });
  }

  async function runBulk(operation: string, fn: () => Promise<BulkOperationResult>) {
    try {
      const result = await fn();
      setLastResult({ operation, result });
      if (result.failed === 0) {
        toast.success(`${operation}: ${result.succeeded}/${result.total} succeeded`);
      } else {
        toast.warning(`${operation}: ${result.succeeded}/${result.total} succeeded, ${result.failed} failed`);
        setShowErrorsModal(true);
      }
      onClearSelection();
    } catch {
      toast.error(`${operation} failed`);
    }
  }

  return (
    <>
      <div className="fixed bottom-6 left-1/2 z-50 flex -translate-x-1/2 items-center gap-3 rounded-xl bg-sidebar-surface px-5 py-3 text-sidebar-heading shadow-2xl">
        <span className="whitespace-nowrap text-sm font-medium">{count} selected</span>
        <Divider />

        <Button
          size="sm"
          variant="secondary"
          leadingIcon={<Play size={14} />}
          title="Start Nodes"
          onClick={() =>
            confirm('Start Nodes', `Start nodes for ${count} devices?`, () =>
              runBulk('Start Nodes', () => startNodes.mutateAsync({ device_ids: ids })),
            )
          }
        >
          Start
        </Button>
        <Button
          size="sm"
          variant="secondary"
          leadingIcon={<Square size={14} />}
          title="Stop Nodes"
          onClick={() =>
            confirm('Stop Nodes', `Stop nodes for ${count} devices?`, () =>
              runBulk('Stop Nodes', () => stopNodes.mutateAsync({ device_ids: ids })),
            )
          }
        >
          Stop
        </Button>
        <Button
          size="sm"
          variant="secondary"
          leadingIcon={<RefreshCw size={14} />}
          title="Restart Nodes"
          onClick={() =>
            confirm('Restart Nodes', `Restart nodes for ${count} devices?`, () =>
              runBulk('Restart Nodes', () => restartNodes.mutateAsync({ device_ids: ids })),
            )
          }
        >
          Restart
        </Button>
        <Button
          size="sm"
          variant="secondary"
          leadingIcon={<Wifi size={14} />}
          title="Reconnect Devices"
          onClick={() =>
            confirm(
              'Reconnect',
              `Reconnect ${count} supported device${count !== 1 ? 's' : ''}?`,
              () => runBulk('Reconnect', () => reconnectMut.mutateAsync({ device_ids: ids })),
            )
          }
        >
          Reconnect
        </Button>

        <Divider />

        <Popover
          open={showAutoManageMenu}
          onOpenChange={setShowAutoManageMenu}
          ariaLabel="Auto-Manage"
          placement={['top-start', 'top-end', 'bottom-start', 'bottom-end']}
          trigger={<span>Auto-Manage</span>}
          triggerClassName="inline-flex items-center justify-center gap-2 rounded-md border border-border-strong bg-surface-1 px-3 py-1.5 text-sm font-medium text-text-2 transition-colors hover:bg-surface-2 focus:outline-none focus:ring-2 focus:ring-accent focus:ring-offset-1 disabled:cursor-not-allowed disabled:opacity-50"
          contentClassName="min-w-[140px] rounded-md border border-border bg-surface-1 py-1 shadow-lg"
        >
          <div role="menu">
            <button
              type="button"
              role="menuitem"
              className="w-full px-3 py-1.5 text-left text-sm text-text-1 hover:bg-surface-2"
              onClick={() => {
                setShowAutoManageMenu(false);
                runBulk('Auto-Manage', () =>
                  setAutoManage.mutateAsync({ device_ids: ids, auto_manage: true }),
                );
              }}
            >
              Enable
            </button>
            <button
              type="button"
              role="menuitem"
              className="w-full px-3 py-1.5 text-left text-sm text-text-1 hover:bg-surface-2"
              onClick={() => {
                setShowAutoManageMenu(false);
                runBulk('Auto-Manage', () =>
                  setAutoManage.mutateAsync({ device_ids: ids, auto_manage: false }),
                );
              }}
            >
              Disable
            </button>
          </div>
        </Popover>

        <Divider />

        <Button
          size="sm"
          variant="secondary"
          leadingIcon={<Wrench size={14} />}
          title="Enter Maintenance"
          onClick={() =>
            confirm('Enter Maintenance', `Put ${count} devices into maintenance mode?`, () =>
              runBulk('Maintenance', () => enterMaintenance.mutateAsync({ device_ids: ids })),
            )
          }
        >
          Maintenance
        </Button>
        <Button
          size="sm"
          variant="secondary"
          leadingIcon={<Power size={14} />}
          title="Exit Maintenance"
          onClick={() =>
            confirm('Exit Maintenance', `Exit maintenance for ${count} devices?`, () =>
              runBulk('Exit Maintenance', () => exitMaintenance.mutateAsync({ device_ids: ids })),
            )
          }
        >
          Exit Maint.
        </Button>
        <Button
          size="sm"
          variant="secondary"
          leadingIcon={<Tags size={14} />}
          title="Update Tags"
          onClick={() => setShowTagsModal(true)}
        >
          Tags
        </Button>

        <Divider />

        <Button
          size="sm"
          variant="danger"
          leadingIcon={<Trash2 size={14} />}
          title="Delete"
          onClick={() =>
            confirm('Delete Devices', `Delete ${count} devices? This cannot be undone.`, () =>
              runBulk('Delete', () => deleteMut.mutateAsync({ device_ids: ids })),
            )
          }
        >
          Delete
        </Button>

        <Divider />
        <button
          type="button"
          onClick={onClearSelection}
          aria-label="Clear selection"
          className="rounded p-1 text-sidebar-text-muted hover:text-sidebar-heading focus:outline-none focus:ring-2 focus:ring-accent"
          title="Clear selection"
        >
          <X size={16} />
        </button>
      </div>

      <ConfirmDialog
        isOpen={!!confirmAction}
        onClose={() => setConfirmAction(null)}
        onConfirm={async () => {
          if (confirmAction) await confirmAction.action();
          setConfirmAction(null);
        }}
        title={confirmAction?.title ?? ''}
        message={confirmAction?.message ?? ''}
        confirmLabel="Confirm"
        variant="danger"
      />

      <TagsActionDialog
        isOpen={showTagsModal}
        onClose={() => setShowTagsModal(false)}
        title="Update Tags"
        tagsText={tagsText}
        merge={mergeTags}
        mergeLabel="Merge with existing tags"
        tagsError={tagsError}
        onTagsTextChange={(value) => {
          setTagsText(value);
          setTagsError(null);
        }}
        onMergeChange={setMergeTags}
        onConfirm={async () => {
          try {
            const tags = parseDeviceActionTags(tagsText);
            await runBulk('Update Tags', () =>
              updateTags.mutateAsync({ device_ids: ids, tags, merge: mergeTags }),
            );
            setShowTagsModal(false);
          } catch (error) {
            setTagsError(error instanceof Error ? error.message : 'Invalid JSON');
          }
        }}
      />

      <DeviceActionErrorsDialog
        isOpen={showErrorsModal}
        onClose={() => setShowErrorsModal(false)}
        title={lastResult ? `${lastResult.operation} Errors` : 'Operation Errors'}
        lines={
          lastResult
            ? Object.entries(lastResult.result.errors).map(([id, error]) => ({
                id,
                label: nameById.get(id) ?? id,
                error,
              }))
            : []
        }
      />
    </>
  );
}
