import { useMemo, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { useDevices, useDevicesPaginated } from '../../hooks/useDevices';
import { useHosts } from '../../hooks/useHosts';
import type {
  ConnectionType,
  DeviceChipStatus,
  DeviceRead,
  DeviceType,
  HardwareHealthStatus,
  HardwareTelemetryState,
  DeviceVerificationUpdate,
} from '../../types';
import {
  CHIP_STATUSES,
  CONNECTION_TYPES,
  DEVICE_TYPES,
  HARDWARE_HEALTH_STATUSES,
  HARDWARE_TELEMETRY_STATES,
} from './devicePageHelpers';
import type { DeviceSortKey } from './devicePageHelpers';
import { deriveDevicesSummaryStats } from './devicesSummary';
import type { DataTableSort } from '../../components/ui/DataTable';

function readEnumSearchParam<T extends string>(searchParams: URLSearchParams, key: string, values: readonly T[]): T | '' {
  const value = searchParams.get(key);
  return value && values.includes(value as T) ? (value as T) : '';
}

export function useDevicesPageController() {
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [sort, setSortState] = useState<DataTableSort<DeviceSortKey>>({ key: 'name', direction: 'asc' });
  const [showAdd, setShowAdd] = useState(false);
  const [verificationRequest, setVerificationRequest] = useState<{
    device: DeviceRead;
    title: string;
    handoffMessage?: string;
    initialExistingForm?: DeviceVerificationUpdate;
  } | null>(null);
  const [editDevice, setEditDevice] = useState<DeviceRead | null>(null);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);

  const platformFilter = searchParams.get('platform_id') ?? '';
  const packIdFilter = searchParams.get('pack_id') ?? '';
  const statusFilter = readEnumSearchParam(searchParams, 'status', CHIP_STATUSES);
  const needsAttentionFilter = searchParams.get('needs_attention') === 'true';
  const deviceTypeFilter = readEnumSearchParam(searchParams, 'device_type', DEVICE_TYPES);
  const connectionTypeFilter = readEnumSearchParam(searchParams, 'connection_type', CONNECTION_TYPES);
  const hardwareHealthStatusFilter = readEnumSearchParam(
    searchParams,
    'hardware_health_status',
    HARDWARE_HEALTH_STATUSES,
  );
  const hardwareTelemetryStateFilter = readEnumSearchParam(
    searchParams,
    'hardware_telemetry_state',
    HARDWARE_TELEMETRY_STATES,
  );
  const osVersionFilter = searchParams.get('os_version') ?? '';
  const search = searchParams.get('search') ?? '';

  const sharedFilters = {
    pack_id: packIdFilter || undefined,
    platform_id: platformFilter || undefined,
    device_type: deviceTypeFilter || undefined,
    connection_type: connectionTypeFilter || undefined,
    os_version: osVersionFilter || undefined,
    search: search || undefined,
    hardware_health_status: hardwareHealthStatusFilter || undefined,
    hardware_telemetry_state: hardwareTelemetryStateFilter || undefined,
  };
  const { data: triageBase = [] } = useDevices(sharedFilters);
  const offset = (page - 1) * pageSize;
  const { data: paginatedResult, isLoading, dataUpdatedAt } = useDevicesPaginated({
    ...sharedFilters,
    status: statusFilter || undefined,
    needs_attention: needsAttentionFilter || undefined,
    limit: pageSize,
    offset,
    sort_by: sort.key,
    sort_dir: sort.direction,
  });
  const devices = paginatedResult?.items ?? [];
  const totalDevices = paginatedResult?.total ?? 0;
  const { data: hosts = [] } = useHosts();

  const hostMap = useMemo(() => new Map(hosts.map((host) => [host.id, host.hostname])), [hosts]);
  const hostOptions = useMemo(
    () => hosts.map((host) => ({ id: host.id, name: host.hostname })),
    [hosts],
  );
  const osVersions = useMemo(
    () => [...new Set(triageBase.map((device) => device.os_version))].sort(),
    [triageBase],
  );

  const needsAttentionCount = useMemo(
    () => triageBase.filter((d) => d.needs_attention).length,
    [triageBase],
  );

  const filtered = devices;
  const summaryStats = useMemo(() => deriveDevicesSummaryStats(triageBase), [triageBase]);

  const sorted = filtered;

  const visibleSelection = useMemo(
    () => new Set(filtered.map((device) => device.id).filter((id) => selectedIds.has(id))),
    [filtered, selectedIds],
  );

  function updateSearchParam(key: string, value: string) {
    setSelectedIds(new Set());
    setPage(1);
    setSearchParams(() => {
      const params = new URLSearchParams(window.location.search);
      if (value) {
        params.set(key, value);
      } else {
        params.delete(key);
      }
      return params;
    });
  }

  function updateAvailabilityFilter(next: DeviceChipStatus | '') {
    updateSearchParam('status', next);
  }

  function updateNeedsAttentionFilter(next: boolean) {
    updateSearchParam('needs_attention', next ? 'true' : '');
  }

  function toggleSelectAll() {
    const allCurrentlySelected =
      filtered.length > 0 && filtered.every((device) => visibleSelection.has(device.id));
    if (allCurrentlySelected) {
      setSelectedIds(new Set());
      return;
    }
    setSelectedIds(new Set(filtered.map((device) => device.id)));
  }

  function toggleSelect(id: string) {
    setSelectedIds((previous) => {
      const next = new Set(previous);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }

  function updatePlatformFilter(next: string) {
    setSelectedIds(new Set());
    setPage(1);
    setSearchParams(() => {
      const params = new URLSearchParams(window.location.search);
      params.delete('platform');
      if (next) {
        params.set('platform_id', next);
      } else {
        params.delete('platform_id');
      }
      return params;
    });
  }

  function updatePackIdFilter(next: string) {
    updateSearchParam('pack_id', next);
  }

  function updateDeviceTypeFilter(next: DeviceType | '') {
    updateSearchParam('device_type', next);
  }

  function updateConnectionTypeFilter(next: ConnectionType | '') {
    updateSearchParam('connection_type', next);
  }

  function updateOsVersionFilter(next: string) {
    updateSearchParam('os_version', next);
  }

  function updateHardwareHealthStatusFilter(next: HardwareHealthStatus | '') {
    updateSearchParam('hardware_health_status', next);
  }

  function updateHardwareTelemetryStateFilter(next: HardwareTelemetryState | '') {
    updateSearchParam('hardware_telemetry_state', next);
  }

  function updateSearch(next: string) {
    updateSearchParam('search', next);
  }

  function clearSelection() {
    setSelectedIds(new Set());
  }

  function handlePageSizeChange(newSize: number) {
    setPageSize(newSize);
    setPage(1);
  }

  function clearFilters() {
    setSelectedIds(new Set());
    setPage(1);
    setSearchParams(new URLSearchParams());
  }

  function setSort(next: DataTableSort<DeviceSortKey>) {
    setSortState(next);
    setPage(1);
  }

  const hasFilters = Boolean(
    packIdFilter || platformFilter || statusFilter || needsAttentionFilter || deviceTypeFilter || connectionTypeFilter ||
    hardwareHealthStatusFilter || hardwareTelemetryStateFilter || osVersionFilter || search,
  );

  return {
    queryClient,
    devices,
    isLoading,
    dataUpdatedAt,
    searchParams,
    hostMap,
    hostOptions,
    osVersions,
    packIdFilter,
    setPackIdFilter: updatePackIdFilter,
    platformFilter,
    setPlatformFilter: updatePlatformFilter,
    statusFilter,
    setAvailabilityFilter: updateAvailabilityFilter,
    needsAttentionFilter,
    setNeedsAttentionFilter: updateNeedsAttentionFilter,
    deviceTypeFilter,
    setDeviceTypeFilter: updateDeviceTypeFilter,
    connectionTypeFilter,
    setConnectionTypeFilter: updateConnectionTypeFilter,
    hardwareHealthStatusFilter,
    setHardwareHealthStatusFilter: updateHardwareHealthStatusFilter,
    hardwareTelemetryStateFilter,
    setHardwareTelemetryStateFilter: updateHardwareTelemetryStateFilter,
    osVersionFilter,
    setOsVersionFilter: updateOsVersionFilter,
    search,
    setSearch: updateSearch,
    needsAttentionCount,
    triageBase,
    filtered,
    summaryStats,
    sorted,
    selectedIds: visibleSelection,
    clearSelection,
    clearFilters,
    hasFilters,
    sort,
    setSort,
    toggleSelectAll,
    toggleSelect,
    showAdd,
    setShowAdd,
    verificationRequest,
    setVerificationRequest,
    editDevice,
    setEditDevice,
    deleteId,
    setDeleteId,
    page,
    setPage,
    pageSize,
    setPageSize: handlePageSizeChange,
    totalDevices,
  };
}
