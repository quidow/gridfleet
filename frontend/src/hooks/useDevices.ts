import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import {
  clearAppiumNodeTransition,
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
  getDeviceTestData,
  replaceDeviceTestData,
  mergeDeviceTestData,
  getTestDataHistory,
} from '../api/devices';
import type { DeviceSortBy, DeviceSortDir } from '../api/devices';
import type {
  ConnectionType,
  DevicePatch,
  HardwareHealthStatus,
  HardwareTelemetryState,
  DeviceChipStatus,
  DeviceType,
  DeviceVerificationCreate,
  DeviceVerificationUpdate,
  DeviceTestData,
} from '../types';
import { useEventStreamStatus } from '../context/EventStreamContext';
import { getErrorMessage } from '../lib/errors';
import {
  invalidatePatchedDeviceQueries,
  patchDeviceQueries,
  rollbackOptimisticDeviceQueries,
  updateEmulatorState,
  updateHold,
  updateNodeOperationalState,
} from '../lib/deviceQueryCache';

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
    staleTime: connected ? 30_000 : 5_000,
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
    staleTime: connected ? 30_000 : 5_000,
    placeholderData: keepPreviousData,
  });
}

export function useDevice(id: string) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: ['device', id],
    queryFn: () => fetchDevice(id),
    refetchInterval: connected ? 60_000 : 5_000,
    staleTime: connected ? 30_000 : 2_500,
    placeholderData: keepPreviousData,
  });
}

export function useDeviceSessionOutcomeHeatmap(id: string, days = 90) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: ['device-session-outcome-heatmap', id, days],
    queryFn: () => fetchDeviceSessionOutcomeHeatmap(id, days),
    refetchInterval: connected ? 60_000 : 15_000,
    staleTime: connected ? 30_000 : 7_500,
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

export function useClearAppiumNodeTransition() {
  const qc = useQueryClient();
  return useMutation({
    mutationKey: ['devices', 'clear-appium-node-transition'],
    mutationFn: ({ nodeId, reason }: { nodeId: string; reason?: string }) =>
      clearAppiumNodeTransition(nodeId, reason),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['devices'] });
      qc.invalidateQueries({ queryKey: ['device'] });
    },
    onError: (error, { nodeId }) => {
      toast.error(getErrorMessage(error, `Failed to clear Appium transition for node ${nodeId}`));
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
    mutationFn: ({ id }: { id: string }) => enterDeviceMaintenance(id),
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
    staleTime: connected ? 30_000 : 7_500,
  });
}

export function useDeviceConfig(id: string) {
  return useQuery({
    queryKey: ['device-config', id],
    queryFn: () => fetchDeviceConfig(id),
    refetchInterval: 30_000,
    staleTime: 15_000,
  });
}

export function useConfigHistory(id: string) {
  return useQuery({
    queryKey: ['config-history', id],
    queryFn: () => fetchConfigHistory(id),
    refetchInterval: false,
    staleTime: Infinity,
  });
}

export function useDeviceTestData(id: string) {
  return useQuery({
    queryKey: ['device-test-data', id],
    queryFn: () => getDeviceTestData(id),
    enabled: Boolean(id),
    refetchInterval: 30_000,
    staleTime: 15_000,
  });
}

export function useTestDataHistory(id: string) {
  return useQuery({
    queryKey: ['test-data-history', id],
    queryFn: () => getTestDataHistory(id),
    enabled: Boolean(id),
    refetchInterval: false,
    staleTime: Infinity,
  });
}

export function useReplaceDeviceTestData(id: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: DeviceTestData) => replaceDeviceTestData(id, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['device-test-data', id] });
      queryClient.invalidateQueries({ queryKey: ['test-data-history', id] });
    },
  });
}

export function useMergeDeviceTestData(id: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: DeviceTestData) => mergeDeviceTestData(id, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['device-test-data', id] });
      queryClient.invalidateQueries({ queryKey: ['test-data-history', id] });
    },
  });
}

export function useDeviceLogs(id: string, lines = 200) {
  return useQuery({
    queryKey: ['device-logs', id, lines],
    queryFn: () => fetchDeviceLogs(id, lines),
    refetchInterval: 5_000,
    staleTime: 2_500,
  });
}

export function useDeviceCapabilities(deviceId: string) {
  return useQuery({
    queryKey: ['device-capabilities', deviceId],
    queryFn: () => fetchDeviceCapabilities(deviceId),
    enabled: !!deviceId,
    refetchInterval: 30_000,
    staleTime: 15_000,
  });
}
