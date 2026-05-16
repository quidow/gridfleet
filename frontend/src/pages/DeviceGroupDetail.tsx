import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { FolderOpen, Plus, Trash2 } from 'lucide-react';
import BulkActionToolbar from './devices/BulkActionToolbar';
import EmptyState from '../components/ui/EmptyState';
import FilterBuilder from './devices/FilterBuilder';
import { LoadingSpinner } from '../components/LoadingSpinner';
import Modal from '../components/ui/Modal';
import GroupActionBar from '../components/GroupActionBar';
import { PlatformIcon } from '../components/PlatformIcon';
import { AvailabilityCell } from './devices/deviceColumns';
import DataTable from '../components/ui/DataTable';
import Button from '../components/ui/Button';
import PageHeader from '../components/ui/PageHeader';
import Card from '../components/ui/Card';
import type { DataTableColumn } from '../components/ui/DataTable';
import { useDevices } from '../hooks/useDevices';
import { useDeviceGroup, useUpdateDeviceGroup, useAddGroupMembers, useRemoveGroupMembers } from '../hooks/useDeviceGroups';
import { useHosts } from '../hooks/useHosts';
import { usePlatformIdLabelMap } from '../hooks/useDriverPacks';
import {
  describeDeviceGroupFilters,
  draftFromDeviceGroupFilters,
  draftToDeviceGroupFilters,
} from '../lib/deviceGroupFilters';
import { resolvePlatformLabel } from '../lib/labels';
import { usePageTitle } from '../hooks/usePageTitle';
import type { DeviceRead } from '../types';

export default function DeviceGroupDetail() {
  const { id } = useParams<{ id: string }>();
  const { data: group, isLoading } = useDeviceGroup(id!);
  const { data: allDevices = [] } = useDevices({});
  const { data: hosts = [] } = useHosts();
  usePageTitle(group?.name ?? 'Group');
  const updateGroup = useUpdateDeviceGroup();
  const addMembers = useAddGroupMembers();
  const removeMembers = useRemoveGroupMembers();
  const platformLabels = usePlatformIdLabelMap();

  const [showAddMembers, setShowAddMembers] = useState(false);
  const [addSelection, setAddSelection] = useState<Set<string>>(new Set());
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [editFilters, setEditFilters] = useState<ReturnType<typeof draftFromDeviceGroupFilters> | null>(null);

  if (isLoading || !group) return <LoadingSpinner />;

  const memberIds = new Set(group.devices.map((device) => device.id));
  const nonMembers = allDevices.filter((device) => !memberIds.has(device.id));
  const isStatic = group.group_type === 'static';
  const hostOptions = hosts.map((host) => ({ id: host.id, name: host.hostname }));
  const hostMap = new Map(hosts.map((host) => [host.id, host.hostname]));
  const osVersionOptions = Array.from(new Set(allDevices.map((device) => device.os_version))).sort();
  const filterSummaries = describeDeviceGroupFilters(group.filters, hostMap, platformLabels);

  const columns: DataTableColumn<DeviceRead>[] = [
    {
      key: 'name',
      header: 'Name',
      render: (device) => (
        <Link to={`/devices/${device.id}`} className="font-medium text-accent hover:text-accent-hover text-sm">
          {device.name}
        </Link>
      ),
    },
    {
      key: 'platform',
      header: 'Platform',
      render: (device) => <PlatformIcon platformId={device.platform_id} platformLabel={device.platform_label} />,
    },
    {
      key: 'os',
      header: 'OS',
      render: (device) => <span className="text-sm text-text-2">{device.os_version}</span>,
    },
    {
      key: 'status',
      header: 'Status',
      render: (device) => <AvailabilityCell device={device} />,
    },
    ...(isStatic
      ? [
          {
            key: 'remove',
            header: 'Remove',
            align: 'right' as const,
            render: (device: DeviceRead) => (
              <button
                onClick={() => removeMembers.mutate({ groupId: group.id, deviceIds: [device.id] })}
                className="rounded p-1.5 text-text-3 hover:text-danger-foreground"
                title="Remove from group"
              >
                <Trash2 size={15} />
              </button>
            ),
          },
        ]
      : []),
  ];

  return (
    <div>
      <PageHeader
        title={group.name}
        subtitle={`${group.group_type} · ${group.device_count} devices${group.description ? ` · ${group.description}` : ''}`}
        actions={
          isStatic ? (
            <Button
              leadingIcon={<Plus size={16} />}
              onClick={() => { setAddSelection(new Set()); setShowAddMembers(true); }}
            >
              Add Devices
            </Button>
          ) : undefined
        }
      />

      <div className="fade-in-stagger flex flex-col gap-6">
      {!isStatic ? (
        <Card padding="md">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-medium text-text-2">Filters</h2>
            {editFilters === null ? (
              <button
                onClick={() => setEditFilters(draftFromDeviceGroupFilters(group.filters))}
                className="text-sm text-accent hover:text-accent-hover"
              >
                Edit
              </button>
            ) : (
              <div className="flex gap-2">
                <button onClick={() => setEditFilters(null)} className="text-sm text-text-3 hover:text-text-2">
                  Cancel
                </button>
                <button
                  onClick={async () => {
                    await updateGroup.mutateAsync({ id: group.id, data: { filters: draftToDeviceGroupFilters(editFilters) } });
                    setEditFilters(null);
                  }}
                  className="text-sm font-medium text-accent hover:text-accent-hover"
                >
                  Save
                </button>
              </div>
            )}
          </div>
          {editFilters ? (
            <FilterBuilder
              filters={editFilters}
              onChange={setEditFilters}
              hostOptions={hostOptions}
              osVersionOptions={osVersionOptions}
              showLabel={false}
            />
          ) : (
            <div className="flex flex-wrap gap-2">
              {filterSummaries.map((item) => (
                <span key={item.key} className="inline-flex items-center rounded-md bg-surface-2 px-2.5 py-1 text-xs font-medium text-text-2">
                  {item.label}: {item.value}
                </span>
              ))}
              {filterSummaries.length === 0 ? (
                <span className="text-sm text-text-3">No filters (matches all devices)</span>
              ) : null}
            </div>
          )}
        </Card>
      ) : null}

      <GroupActionBar groupId={group.id} devices={group.devices} />

      <DataTable<DeviceRead>
        columns={columns}
        rows={group.devices}
        rowKey={(d) => d.id}
        selection={
          isStatic
            ? {
                selectedKeys: selectedIds,
                onToggle: (device) => {
                  setSelectedIds((prev) => {
                    const next = new Set(prev);
                    if (next.has(device.id)) next.delete(device.id);
                    else next.add(device.id);
                    return next;
                  });
                },
                onToggleAll: (devices) => {
                  setSelectedIds(
                    selectedIds.size === devices.length
                      ? new Set()
                      : new Set(devices.map((d) => d.id)),
                  );
                },
              }
            : undefined
        }
        emptyState={
          <EmptyState
            icon={FolderOpen}
            title={isStatic ? 'No devices in this group yet' : 'No devices match these filters'}
            description={
              isStatic
                ? 'Add devices to build out this static group and run bulk actions against them.'
                : 'Adjust the filters to widen the match set or review your device inventory.'
            }
            action={
              isStatic ? (
                <Button
                  leadingIcon={<Plus size={16} />}
                  onClick={() => { setAddSelection(new Set()); setShowAddMembers(true); }}
                >
                  Add Devices
                </Button>
              ) : (
                <Button
                  variant="secondary"
                  onClick={() => setEditFilters(draftFromDeviceGroupFilters(group.filters))}
                >
                  Edit Filters
                </Button>
              )
            }
          />
        }
      />
      </div>

      {selectedIds.size > 0 ? (
        <BulkActionToolbar
          selectedIds={selectedIds}
          selectedDevices={group.devices.filter((device) => selectedIds.has(device.id))}
          onClearSelection={() => setSelectedIds(new Set())}
        />
      ) : null}

      <Modal isOpen={showAddMembers} onClose={() => setShowAddMembers(false)} title="Add Devices to Group">
        <div className="max-h-80 space-y-1 overflow-y-auto">
          {nonMembers.length === 0 ? (
            <p className="py-4 text-center text-sm text-text-3">All devices are already in this group.</p>
          ) : (
            nonMembers.map((device) => (
              <label key={device.id} className="flex cursor-pointer items-center gap-3 rounded px-3 py-2 hover:bg-surface-2">
                <input
                  type="checkbox"
                  checked={addSelection.has(device.id)}
                  onChange={() => {
                    setAddSelection((previous) => {
                      const next = new Set(previous);
                      if (next.has(device.id)) next.delete(device.id);
                      else next.add(device.id);
                      return next;
                    });
                  }}
                  className="h-4 w-4 rounded border-border-strong text-accent focus:ring-accent"
                />
                <span className="text-sm">{device.name}</span>
                <span className="text-xs text-text-3">
                  {resolvePlatformLabel(device.platform_id, device.platform_label)} - {device.os_version}
                </span>
              </label>
            ))
          )}
        </div>
        <div className="mt-4 flex justify-end gap-3 border-t pt-4">
          <Button variant="secondary" onClick={() => setShowAddMembers(false)}>
            Cancel
          </Button>
          <Button
            disabled={addSelection.size === 0 || addMembers.isPending}
            loading={addMembers.isPending}
            onClick={async () => {
              await addMembers.mutateAsync({ groupId: group.id, deviceIds: Array.from(addSelection) });
              setShowAddMembers(false);
            }}
          >
            {addMembers.isPending ? 'Adding...' : `Add ${addSelection.size} Devices`}
          </Button>
        </div>
      </Modal>
    </div>
  );
}
