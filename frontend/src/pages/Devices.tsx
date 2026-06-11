import { useCallback, useMemo, useState } from 'react';
import { FileDown, Plus, SearchX, Smartphone } from 'lucide-react';
import {
  useDeleteDevice,
  useEnterDeviceMaintenance,
  useExitDeviceMaintenance,
  useRestartNode,
  useStartNode,
  useStopNode,
} from '../hooks/useDevices';
import { LoadingSpinner } from '../components/LoadingSpinner';
import { BulkActionToolbar } from './devices/BulkActionToolbar';
import { AddDeviceModal } from './devices/AddDeviceModal';
import { SetupVerificationModal } from './devices/SetupVerificationModal';
import { ConfirmDialog } from '../components/ui/ConfirmDialog';
import { DeviceEditModal } from './devices/DeviceEditModal';
import { DevicesFiltersBar } from './devices/DevicesFiltersBar';
import { DevicesTable } from './devices/DevicesTable';
import { DevicesSummaryPills } from './devices/DevicesSummaryPills';
import { NoDriverPacksBanner } from '../components/NoDriverPacksBanner';
import {
  getVerificationAction,
} from './devices/devicePageHelpers';
import { useDevicesPageController } from './devices/useDevicesPageController';
import { getPendingDeviceAction, type DevicePendingAction } from '../lib/devicePendingAction';
import { getVerificationAction as getWorkflowVerificationAction } from '../lib/deviceWorkflow';
import { usePageTitle } from '../hooks/usePageTitle';
import { useDevRenderCrashTrigger } from '../hooks/useDevRenderCrashTrigger';
import { useDriverPackCatalog } from '../hooks/useDriverPacks';
import { Card } from '../components/ui/Card';
import { PageHeader } from '../components/ui/PageHeader';
import { Button } from '../components/ui/Button';
import { ListPageSubheader } from '../components/ui/ListPageSubheader';
import { DeviceInventoryExportModal } from '../components/devices/DeviceInventoryExportModal';
import { Pagination } from '../components/ui/Pagination';
import type { DeviceAction } from './devices/deviceActions';
import { qk } from '../lib/queryKeys';
import { DashedEmptyPanel } from '../components/ui/DashedEmptyPanel';

function DevicesEmptyPanel({
  hasFilters,
  onAddDevice,
  onClearFilters,
}: {
  hasFilters: boolean;
  onAddDevice: () => void;
  onClearFilters: () => void;
}) {
  const Icon = hasFilters ? SearchX : Smartphone;
  const title = hasFilters ? 'No matching devices' : 'No devices registered';
  const description = hasFilters
    ? 'Current filters hide every device in this fleet.'
    : 'Register a device to start routing sessions through Grid.';
  const action = hasFilters ? (
    <Button variant="secondary" onClick={onClearFilters}>
      Clear Filters
    </Button>
  ) : (
    <Button onClick={onAddDevice} leadingIcon={<Plus size={16} />}>
      Register Device
    </Button>
  );

  return <DashedEmptyPanel icon={Icon} title={title} description={description} action={action} />;
}

export function Devices() {
  useDevRenderCrashTrigger('devices-page');
  usePageTitle('Devices');
  const controller = useDevicesPageController();
  const deleteDevice = useDeleteDevice();
  const enterMaintenance = useEnterDeviceMaintenance();
  const exitMaintenance = useExitDeviceMaintenance();
  const startNode = useStartNode();
  const stopNode = useStopNode();
  const restartNode = useRestartNode();
  const { data: catalog = [] } = useDriverPackCatalog();
  const enabledPackCount = catalog.filter((pack) => pack.state === 'enabled').length;
  const [inventoryOpen, setInventoryOpen] = useState(false);

  const inventoryFilters = useMemo(() => {
    const out: Record<string, string> = {};
    const skip = new Set(['sort_by', 'sort_dir', 'page', 'page_size']);
    for (const [key, value] of controller.searchParams.entries()) {
      if (skip.has(key)) continue;
      if (value === '') continue;
      out[key] = value;
    }
    return out;
  }, [controller.searchParams]);

  const handleDeviceAction = useCallback((action: DeviceAction) => {
    switch (action.type) {
      case 'enter-maintenance':
        enterMaintenance.mutate({ id: action.deviceId });
        break;
      case 'exit-maintenance':
        exitMaintenance.mutate(action.deviceId);
        break;
      case 'start-node':
        startNode.mutate(action.deviceId);
        break;
      case 'stop-node':
        stopNode.mutate(action.deviceId);
        break;
      case 'restart-node':
        restartNode.mutate(action.deviceId);
        break;
      case 'verify':
        controller.setVerificationRequest({
          device: action.device,
          ...getVerificationAction(action.device),
        });
        break;
      case 'edit':
        controller.setEditDevice(action.device);
        break;
      case 'delete':
        controller.setDeleteId(action.deviceId);
        break;
    }
  }, [enterMaintenance, exitMaintenance, startNode, stopNode, restartNode, controller]);

  const showInitialLoading = controller.isLoading && controller.devices.length === 0;
  const hostCount = controller.hostMap.size;
  const deviceSubtitle = `${controller.triageBase.length} registered across ${hostCount} host${hostCount !== 1 ? 's' : ''}`;
  const filteredCount = controller.filtered.length;
  const totalCount = controller.totalDevices;
  const selectedCount = controller.selectedIds.size;
  const showingLabel =
    filteredCount === totalCount
      ? `Showing ${filteredCount} device${filteredCount !== 1 ? 's' : ''}`
      : `Showing ${filteredCount} of ${totalCount} devices`;
  const subheaderMeta = selectedCount > 0 ? `${selectedCount} selected` : undefined;

  function getPendingAction(deviceId: string): DevicePendingAction | null {
    return getPendingDeviceAction(deviceId, [
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
    ]);
  }

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Devices"
        subtitle={deviceSubtitle}
        updatedAt={controller.dataUpdatedAt}
        summary={
          <DevicesSummaryPills
            stats={controller.summaryStats}
            searchParams={controller.searchParams}
            isLoading={showInitialLoading}
          />
        }
      />

      <div className="fade-in-stagger flex min-h-0 flex-1 flex-col">
        <div className="mb-4">
          <NoDriverPacksBanner packCount={enabledPackCount} />
        </div>

        <DevicesFiltersBar
          packIdFilter={controller.packIdFilter}
          onPackIdFilterChange={controller.setPackIdFilter}
          platformFilter={controller.platformFilter}
          onPlatformFilterChange={controller.setPlatformFilter}
          deviceTypeFilter={controller.deviceTypeFilter}
          onDeviceTypeFilterChange={controller.setDeviceTypeFilter}
          connectionTypeFilter={controller.connectionTypeFilter}
          onConnectionTypeFilterChange={controller.setConnectionTypeFilter}
          hardwareHealthStatusFilter={controller.hardwareHealthStatusFilter}
          onHardwareHealthStatusFilterChange={controller.setHardwareHealthStatusFilter}
          hardwareTelemetryStateFilter={controller.hardwareTelemetryStateFilter}
          onHardwareTelemetryStateFilterChange={controller.setHardwareTelemetryStateFilter}
          deviceHealthFilter={controller.deviceHealthFilter}
          onDeviceHealthFilterChange={controller.setDeviceHealthFilter}
          nodeHealthFilter={controller.nodeHealthFilter}
          onNodeHealthFilterChange={controller.setNodeHealthFilter}
          viabilityFilter={controller.viabilityFilter}
          onViabilityFilterChange={controller.setViabilityFilter}
          osVersionFilter={controller.osVersionFilter}
          onOsVersionFilterChange={controller.setOsVersionFilter}
          osVersions={controller.osDisplayVersions}
          search={controller.search}
          onSearchChange={controller.setSearch}
          onClear={controller.hasFilters ? controller.clearFilters : undefined}
        />

        <ListPageSubheader
          title={showingLabel}
          meta={subheaderMeta}
          action={
            <div className="flex items-center gap-2">
              <Button
                variant="secondary"
                onClick={() => setInventoryOpen(true)}
                leadingIcon={<FileDown size={16} />}
              >
                Export Inventory
              </Button>
              <Button onClick={() => controller.setShowAdd(true)} leadingIcon={<Plus size={16} />}>
                Add Device
              </Button>
            </div>
          }
        />

        {showInitialLoading ? (
          <Card padding="none" className="py-12">
            <LoadingSpinner />
          </Card>
        ) : controller.sorted.length === 0 ? (
          <DevicesEmptyPanel
            hasFilters={controller.hasFilters}
            onAddDevice={() => controller.setShowAdd(true)}
            onClearFilters={controller.clearFilters}
          />
        ) : (
          <DevicesTable
            devices={controller.sorted}
            selectedIds={controller.selectedIds}
            hostMap={controller.hostMap}
            sort={controller.sort}
            pendingActionForDevice={getPendingAction}
            onSortChange={controller.setSort}
            onToggleSelectAll={controller.toggleSelectAll}
            onToggleSelect={controller.toggleSelect}
            onAction={handleDeviceAction}
          />
        )}

        {controller.sorted.length > 0 ? (
          <Pagination
            page={controller.page}
            pageSize={controller.pageSize}
            total={controller.totalDevices}
            onPageChange={controller.setPage}
            onPageSizeChange={controller.setPageSize}
          />
        ) : null}
      </div>

      {controller.selectedIds.size > 0 ? (
        <BulkActionToolbar
          selectedIds={controller.selectedIds}
          selectedDevices={controller.sorted.filter((device) => controller.selectedIds.has(device.id))}
          onClearSelection={controller.clearSelection}
        />
      ) : null}

      {controller.showAdd ? (
        <AddDeviceModal
          isOpen={controller.showAdd}
          onClose={() => controller.setShowAdd(false)}
          hostOptions={controller.hostOptions}
          onCompleted={() => {
            controller.queryClient.invalidateQueries({ queryKey: qk.devices.root });
          }}
        />
      ) : null}

      {controller.verificationRequest ? (
        <SetupVerificationModal
          isOpen={controller.verificationRequest !== null}
          onClose={() => controller.setVerificationRequest(null)}
          existingDevice={controller.verificationRequest.device}
          initialExistingForm={controller.verificationRequest.initialExistingForm}
          onCompleted={() => controller.setVerificationRequest(null)}
          handoffMessage={controller.verificationRequest.handoffMessage}
          title={
            controller.verificationRequest.title
            ?? getWorkflowVerificationAction(controller.verificationRequest.device.readiness_state).title
          }
        />
      ) : null}

      <DeviceEditModal
        device={controller.editDevice}
        hostMap={controller.hostMap}
        onClose={() => controller.setEditDevice(null)}
        onRequestVerification={controller.setVerificationRequest}
      />

      <ConfirmDialog
        isOpen={!!controller.deleteId}
        onClose={() => controller.setDeleteId(null)}
        onConfirm={() => {
          if (controller.deleteId) {
            deleteDevice.mutate(controller.deleteId);
          }
        }}
        title="Delete Device"
        message="Are you sure you want to delete this device? This action cannot be undone."
        confirmLabel="Delete"
        variant="danger"
      />

      <DeviceInventoryExportModal
        isOpen={inventoryOpen}
        onClose={() => setInventoryOpen(false)}
        filters={inventoryFilters}
      />
    </div>
  );
}
