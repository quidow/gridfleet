import { useMemo } from 'react';
import DataTable, { type DataTableSort } from '../../components/ui/DataTable';
import type { DeviceRead } from '../../types';
import type { DevicePendingAction } from '../../lib/devicePendingAction';
import type { DeviceSortKey } from './devicePageHelpers';
import type { DeviceAction } from './deviceActions';
import { buildDeviceColumns, buildDeviceMenuItems } from './deviceColumns';

type Props = {
  devices: DeviceRead[];
  selectedIds: Set<string>;
  hostMap: Map<string, string>;
  sort: DataTableSort<DeviceSortKey>;
  pendingActionForDevice: (id: string) => DevicePendingAction | null;
  onSortChange: (next: DataTableSort<DeviceSortKey>) => void;
  onToggleSelectAll: () => void;
  onToggleSelect: (id: string) => void;
  onAction: (action: DeviceAction) => void;
};

export default function DevicesTable({
  devices,
  selectedIds,
  hostMap,
  sort,
  pendingActionForDevice,
  onSortChange,
  onToggleSelectAll,
  onToggleSelect,
  onAction,
}: Props) {
  const columns = useMemo(
    () => buildDeviceColumns({ hostMap, pendingActionForDevice, onAction }),
    [hostMap, pendingActionForDevice, onAction],
  );

  return (
    <div className="devices-table-shell">
      <DataTable<DeviceRead, DeviceSortKey>
        columns={columns}
        rows={devices}
        rowKey={(device) => device.id}
        sort={sort}
        onSortChange={onSortChange}
        stickyHeader
        caption="Devices"
        rowTestId={(device) => `device-row-${device.id}`}
        selection={{
          selectedKeys: selectedIds,
          onToggle: (device) => onToggleSelect(device.id),
          onToggleAll: () => onToggleSelectAll(),
        }}
        rowActions={(device) =>
          buildDeviceMenuItems(device, pendingActionForDevice(device.id), onAction)
        }
        rowActionsLabel={(device) => `Row actions for ${device.name}`}
      />
    </div>
  );
}
