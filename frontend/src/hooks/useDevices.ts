import { keepPreviousData, QueryClient, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import {
  deleteDevice,
  fetchConfigHistory,
  fetchDevice,
  fetchDeviceCapabilities,
  fetchDeviceConfig,
  fetchDeviceHealth,
  fetchDeviceSessionOutcomeHeatmap,
  fetchDeviceLogs,
  fetchDevices,
  fetchDevicesPaginated,
  enterDeviceMaintenance,
  exitDeviceMaintenance,
  reconnectDevice,
  runDeviceLifecycleAction,
  runDeviceSessionTest,
  restartNode,
  startExistingDeviceVerificationJob,
  startDeviceVerificationJob,
  startNode,
  stopNode,
  updateDevice,
} from '../api/devices';
import type { DeviceSortBy, DeviceSortDir } from '../api/devices';
import type {
  ConnectionType,
  DevicePatch,
  HardwareHealthStatus,
  HardwareTelemetryState,
  DeviceRead,
  DeviceChipStatus,
  DeviceDetail,
  DeviceType,
  DeviceVerificationCreate,
  DeviceVerificationUpdate,
  NodeState,
} from '../types';
import { useEventStreamStatus } from '../context/EventStreamContext';

import type { PaginatedResponse } from '../types/shared';

type DeviceListData = DeviceRead[] | PaginatedResponse<DeviceRead>;
type DeviceQuerySnapshot = Array<[readonly unknown[], DeviceListData | undefined]>;

type OptimisticDeviceContext = {
  deviceId: string;
  devicesSnapshots: DeviceQuerySnapshot;
  deviceSnapshot: DeviceDetail | undefined;
};

type DeviceCacheUpdater = <T extends DeviceRead>(device: T) => T;

function getErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return fallback;
}

function waitForNextPaint(minimumDelayMs = 0): Promise<void> {
  if (typeof window === 'undefined' || typeof window.requestAnimationFrame !== 'function') {
    return Promise.resolve();
  }

  return new Promise((resolve) => {
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        if (minimumDelayMs > 0) {
          window.setTimeout(resolve, minimumDelayMs);
          return;
        }
        resolve();
      });
    });
  });
}

function rollbackOptimisticDeviceQueries(
  qc: QueryClient,
  context: OptimisticDeviceContext | undefined,
) {
  if (!context) {
    return;
  }

  for (const [queryKey, snapshot] of context.devicesSnapshots) {
    qc.setQueryData<DeviceListData>(queryKey, snapshot);
  }

  qc.setQueryData(['device', context.deviceId], context.deviceSnapshot);
}

async function patchDeviceQueries(
  qc: QueryClient,
  deviceId: string,
  updater: DeviceCacheUpdater,
): Promise<OptimisticDeviceContext> {
  await Promise.all([
    qc.cancelQueries({ queryKey: ['devices'] }),
    qc.cancelQueries({ queryKey: ['device', deviceId] }),
  ]);

  const devicesSnapshots = qc.getQueriesData<DeviceListData>({ queryKey: ['devices'] });
  for (const [queryKey, snapshot] of devicesSnapshots) {
    if (!snapshot) {
      continue;
    }
    if (Array.isArray(snapshot)) {
      qc.setQueryData<DeviceRead[]>(
        queryKey,
        snapshot.map((device) => (device.id === deviceId ? updater(device) : device)),
      );
    } else {
      qc.setQueryData<PaginatedResponse<DeviceRead>>(queryKey, {
        ...snapshot,
        items: snapshot.items.map((device) => (device.id === deviceId ? updater(device) : device)),
      });
    }
  }

  const deviceKey = ['device', deviceId] as const;
  const deviceSnapshot = qc.getQueryData<DeviceDetail>(deviceKey);
  if (deviceSnapshot) {
    qc.setQueryData<DeviceDetail>(deviceKey, updater(deviceSnapshot));
  }

  return {
    deviceId,
    devicesSnapshots,
    deviceSnapshot,
  };
}

function invalidatePatchedDeviceQueries(
  qc: QueryClient,
  deviceId: string,
) {
  qc.invalidateQueries({ queryKey: ['devices'] });
  qc.invalidateQueries({ queryKey: ['device', deviceId] });
}

function updateAutoManage(autoManage: boolean): DeviceCacheUpdater {
  return <T extends DeviceRead>(device: T): T => ({
    ...device,
    auto_manage: autoManage,
  });
}

function updateHold(hold: DeviceRead['hold']): DeviceCacheUpdater {
  return <T extends DeviceRead>(device: T): T => ({
    ...device,
    hold,
  });
}

function updateEmulatorState(state: string): DeviceCacheUpdater {
  return <T extends DeviceRead>(device: T): T => ({
    ...device,
    emulator_state: state,
  });
}

function updateNodeOperationalState(
  operationalState: DeviceRead['operational_state'],
  nodeState: NodeState,
): DeviceCacheUpdater {
  return <T extends DeviceRead>(device: T): T => {
    const nextDevice = {
      ...device,
      operational_state: operationalState,
    } as T & Partial<DeviceDetail>;

    if (nextDevice.appium_node) {
      nextDevice.appium_node = {
        ...nextDevice.appium_node,
        state: nodeState,
        pid: nodeState === 'running' ? nextDevice.appium_node.pid : null,
      };
    }

    return nextDevice as T;
  };
}

export function useDevices(params?: {
  pack_id?: string;
  platform_id?: string;
  status?: DeviceChipStatus;
  host_id?: string;
  device_type?: DeviceType;
  connection_type?: ConnectionType;
  os_version?: string;
  search?: string;
  hardware_health_status?: HardwareHealthStatus;
  hardware_telemetry_state?: HardwareTelemetryState;
  needs_attention?: boolean;
}) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: ['devices', params],
    queryFn: () => fetchDevices(params),
    refetchInterval: connected ? 60_000 : 10_000,
  });
}

export function useDevicesPaginated(params: {
  pack_id?: string;
  platform_id?: string;
  status?: DeviceChipStatus;
  host_id?: string;
  device_type?: DeviceType;
  connection_type?: ConnectionType;
  os_version?: string;
  search?: string;
  hardware_health_status?: HardwareHealthStatus;
  hardware_telemetry_state?: HardwareTelemetryState;
  needs_attention?: boolean;
  limit: number;
  offset: number;
  sort_by?: DeviceSortBy;
  sort_dir?: DeviceSortDir;
}) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: ['devices', params],
    queryFn: () => fetchDevicesPaginated(params),
    refetchInterval: connected ? 60_000 : 10_000,
    placeholderData: keepPreviousData,
  });
}

export function useDevice(id: string) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: ['device', id],
    queryFn: () => fetchDevice(id),
    refetchInterval: connected ? 60_000 : 5_000,
    placeholderData: keepPreviousData,
  });
}

export function useDeviceSessionOutcomeHeatmap(id: string, days = 90) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: ['device-session-outcome-heatmap', id, days],
    queryFn: () => fetchDeviceSessionOutcomeHeatmap(id, days),
    refetchInterval: connected ? 60_000 : 15_000,
    enabled: !!id,
  });
}

export function useStartDeviceVerification() {
  return useMutation({
    mutationFn: (body: DeviceVerificationCreate) => startDeviceVerificationJob(body),
  });
}

export function useStartExistingDeviceVerification() {
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: DeviceVerificationUpdate }) =>
      startExistingDeviceVerificationJob(id, body),
  });
}

export function useUpdateDevice() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: DevicePatch }) => updateDevice(id, body),
    onSuccess: (_data, { id }) => {
      qc.invalidateQueries({ queryKey: ['devices'] });
      qc.invalidateQueries({ queryKey: ['device', id] });
    },
  });
}

export function useToggleDeviceAutoManage() {
  const qc = useQueryClient();
  return useMutation({
    mutationKey: ['devices', 'toggle-auto-manage'],
    mutationFn: ({ id, autoManage }: { id: string; autoManage: boolean }) =>
      updateDevice(id, { auto_manage: autoManage }),
    onMutate: async ({ id, autoManage }) => {
      const context = await patchDeviceQueries(qc, id, updateAutoManage(autoManage));
      await waitForNextPaint(150);
      return context;
    },
    onError: (error, { id }, context) => {
      rollbackOptimisticDeviceQueries(qc, context);
      toast.error(getErrorMessage(error, `Failed to update auto-manage for device ${id}`));
    },
    onSettled: (_data, _error, { id }) => {
      invalidatePatchedDeviceQueries(qc, id);
    },
  });
}

export function useDeleteDevice() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteDevice(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['devices'] }),
  });
}

export function useStartNode() {
  const qc = useQueryClient();
  return useMutation({
    mutationKey: ['devices', 'start-node'],
    mutationFn: (id: string) => startNode(id),
    onMutate: (id) => patchDeviceQueries(qc, id, updateNodeOperationalState('available', 'running')),
    onError: (error, id, context) => {
      rollbackOptimisticDeviceQueries(qc, context);
      toast.error(getErrorMessage(error, `Failed to start node for device ${id}`));
    },
    onSettled: (_data, _error, id) => {
      invalidatePatchedDeviceQueries(qc, id);
    },
  });
}

export function useStopNode() {
  const qc = useQueryClient();
  return useMutation({
    mutationKey: ['devices', 'stop-node'],
    mutationFn: (id: string) => stopNode(id),
    onMutate: (id) => patchDeviceQueries(qc, id, updateNodeOperationalState('offline', 'stopped')),
    onError: (error, id, context) => {
      rollbackOptimisticDeviceQueries(qc, context);
      toast.error(getErrorMessage(error, `Failed to stop node for device ${id}`));
    },
    onSettled: (_data, _error, id) => {
      invalidatePatchedDeviceQueries(qc, id);
    },
  });
}

export function useRestartNode() {
  const qc = useQueryClient();
  return useMutation({
    mutationKey: ['devices', 'restart-node'],
    mutationFn: (id: string) => restartNode(id),
    onMutate: (id) => patchDeviceQueries(qc, id, updateNodeOperationalState('available', 'running')),
    onError: (error, id, context) => {
      rollbackOptimisticDeviceQueries(qc, context);
      toast.error(getErrorMessage(error, `Failed to restart node for device ${id}`));
    },
    onSettled: (_data, _error, id) => {
      invalidatePatchedDeviceQueries(qc, id);
    },
  });
}

export function useReconnectDevice() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => reconnectDevice(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['devices'] });
      qc.invalidateQueries({ queryKey: ['device'] });
    },
  });
}

export function useRunDeviceLifecycleAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationKey: ['devices', 'lifecycle-action'],
    mutationFn: ({ id, action, args }: { id: string; action: string; args?: Record<string, unknown> }) =>
      runDeviceLifecycleAction(id, action, args),
    onMutate: ({ id, action }) => {
      const optimisticState = action === 'boot' ? 'booting' : action === 'shutdown' ? 'shutdown' : null;
      return optimisticState ? patchDeviceQueries(qc, id, updateEmulatorState(optimisticState)) : undefined;
    },
    onSuccess: (_data, { action }) => {
      toast.success(`Lifecycle action ${action.replaceAll('_', ' ')} queued`);
    },
    onError: (error, { id, action }, context) => {
      rollbackOptimisticDeviceQueries(qc, context);
      toast.error(getErrorMessage(error, `Failed to run lifecycle action ${action} for device ${id}`));
    },
    onSettled: (_data, _error, { id }) => {
      invalidatePatchedDeviceQueries(qc, id);
    },
  });
}

export function useEnterDeviceMaintenance() {
  const qc = useQueryClient();
  return useMutation({
    mutationKey: ['devices', 'enter-maintenance'],
    mutationFn: ({ id, drain }: { id: string; drain?: boolean }) => enterDeviceMaintenance(id, drain),
    onMutate: ({ id }) => patchDeviceQueries(qc, id, updateHold('maintenance')),
    onError: (error, { id }, context) => {
      rollbackOptimisticDeviceQueries(qc, context);
      toast.error(getErrorMessage(error, `Failed to enter maintenance for device ${id}`));
    },
    onSettled: (_data, _error, { id }) => {
      invalidatePatchedDeviceQueries(qc, id);
    },
  });
}

export function useExitDeviceMaintenance() {
  const qc = useQueryClient();
  return useMutation({
    mutationKey: ['devices', 'exit-maintenance'],
    mutationFn: (id: string) => exitDeviceMaintenance(id),
    onMutate: (id) => patchDeviceQueries(qc, id, updateHold(null)),
    onError: (error, id, context) => {
      rollbackOptimisticDeviceQueries(qc, context);
      toast.error(getErrorMessage(error, `Failed to exit maintenance for device ${id}`));
    },
    onSettled: (_data, _error, id) => {
      invalidatePatchedDeviceQueries(qc, id);
    },
  });
}

export function useRunDeviceSessionTest() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => runDeviceSessionTest(id),
    onSuccess: (_data, id) => {
      qc.invalidateQueries({ queryKey: ['device-health', id] });
      qc.invalidateQueries({ queryKey: ['device', id] });
    },
  });
}

export function useDeviceHealth(id: string) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: ['device-health', id],
    queryFn: () => fetchDeviceHealth(id),
    refetchInterval: connected ? 60_000 : 15_000,
  });
}

export function useDeviceConfig(id: string) {
  return useQuery({
    queryKey: ['device-config', id],
    queryFn: () => fetchDeviceConfig(id),
  });
}

export function useConfigHistory(id: string) {
  return useQuery({
    queryKey: ['config-history', id],
    queryFn: () => fetchConfigHistory(id),
  });
}

export function useDeviceLogs(id: string, lines = 200) {
  return useQuery({
    queryKey: ['device-logs', id, lines],
    queryFn: () => fetchDeviceLogs(id, lines),
    refetchInterval: 5_000,
  });
}

export function useDeviceCapabilities(deviceId: string) {
  return useQuery({
    queryKey: ['device-capabilities', deviceId],
    queryFn: () => fetchDeviceCapabilities(deviceId),
    enabled: !!deviceId,
  });
}
