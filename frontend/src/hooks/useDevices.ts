import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
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
  startNode,
  stopNode,
  updateDevice,
  getDeviceTestData,
  replaceDeviceTestData,
  getTestDataHistory,
} from '../api/devices';
import { startDeviceVerificationJob, startExistingDeviceVerificationJob } from '../api/verification';
import type { DeviceSortBy, DeviceSortDir } from '../api/devices';
import type {
  ConnectionType,
  DevicePatch,
  HealthVerdictStatus,
  DeviceFilterStatus,
  DeviceType,
  DeviceVerificationCreate,
  DeviceVerificationUpdate,
  DeviceTestData,
} from '../types';
import { useEventStreamStatus } from '../context/EventStreamContext';
import { sseAdaptivePolling } from './polling';
import { POLL_FAST_MS, POLL_DEFAULT_MS, POLL_RELAXED_MS, POLL_SLOW_MS } from './polling';
import { qk } from '../lib/queryKeys';
import { getErrorMessage } from '../lib/errors';
import {
  invalidatePatchedDeviceQueries,
  patchDeviceQueries,
  rollbackOptimisticDeviceQueries,
  updateOperationalState,
  updateNodeOperationalState,
} from '../lib/deviceQueryCache';

export function useDevices(params?: {
  pack_id?: string;
  platform_id?: string;
  group?: string[];
  status?: DeviceFilterStatus;
  reserved?: boolean;
  host_id?: string;
  device_type?: DeviceType;
  connection_type?: ConnectionType;
  os_version?: string;
  search?: string;
  needs_attention?: boolean;
  device_health?: HealthVerdictStatus;
  node_health?: HealthVerdictStatus;
  viability?: HealthVerdictStatus;
}) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.devices.list(params),
    queryFn: () => fetchDevices(params),
    ...sseAdaptivePolling(connected, POLL_DEFAULT_MS),
  });
}

export function useDevicesPaginated(params: {
  pack_id?: string;
  platform_id?: string;
  group?: string[];
  status?: DeviceFilterStatus;
  reserved?: boolean;
  host_id?: string;
  device_type?: DeviceType;
  connection_type?: ConnectionType;
  os_version?: string;
  search?: string;
  needs_attention?: boolean;
  device_health?: HealthVerdictStatus;
  node_health?: HealthVerdictStatus;
  viability?: HealthVerdictStatus;
  limit: number;
  offset: number;
  sort_by?: DeviceSortBy;
  sort_dir?: DeviceSortDir;
}) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.devices.list(params),
    queryFn: () => fetchDevicesPaginated(params),
    ...sseAdaptivePolling(connected, POLL_DEFAULT_MS),
    placeholderData: keepPreviousData,
  });
}

export function useDevice(id: string) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.device.detail(id),
    queryFn: () => fetchDevice(id),
    ...sseAdaptivePolling(connected, POLL_FAST_MS),
    placeholderData: keepPreviousData,
  });
}

export function useDeviceSessionOutcomeHeatmap(id: string, days = 90) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.deviceSessionOutcomeHeatmap.byDevice(id, days),
    queryFn: () => fetchDeviceSessionOutcomeHeatmap(id, days),
    ...sseAdaptivePolling(connected, POLL_RELAXED_MS),
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
      qc.invalidateQueries({ queryKey: qk.devices.root });
      qc.invalidateQueries({ queryKey: qk.device.detail(id) });
    },
  });
}

export function useDeleteDevice() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteDevice(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.devices.root }),
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
      qc.invalidateQueries({ queryKey: qk.devices.root });
      qc.invalidateQueries({ queryKey: qk.device.root });
    },
  });
}

export function useRunDeviceLifecycleAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationKey: ['devices', 'lifecycle-action'],
    mutationFn: ({ id, action, args }: { id: string; action: string; args?: Record<string, unknown> }) =>
      runDeviceLifecycleAction(id, action, args),
    onSuccess: (_data, { action }) => {
      toast.success(`Lifecycle action ${action.replaceAll('_', ' ')} queued`);
    },
    onError: (error, { id, action }) => {
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
    onMutate: ({ id }) => patchDeviceQueries(qc, id, updateOperationalState('maintenance')),
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
    onError: (error, id) => {
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
      qc.invalidateQueries({ queryKey: qk.deviceHealth.byDevice(id) });
      qc.invalidateQueries({ queryKey: qk.device.detail(id) });
    },
    onError: (error) => {
      // A 409 means a viability probe is already in flight for this device — a
      // scheduled sweep, another operator, or a lock still clearing. That is not a
      // failure of this device, so surface it as an informational notice instead of
      // letting the rejected request bubble up as a raw console error.
      if ((error as { status?: number }).status === 409) {
        toast.message('A session probe is already running for this device — try again in a moment.');
        return;
      }
      toast.error(getErrorMessage(error, 'Session test failed'));
    },
  });
}

export function useDeviceHealth(id: string) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.deviceHealth.byDevice(id),
    queryFn: () => fetchDeviceHealth(id),
    ...sseAdaptivePolling(connected, POLL_RELAXED_MS),
  });
}

export function useDeviceConfig(id: string) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.deviceConfig.byDevice(id),
    queryFn: () => fetchDeviceConfig(id),
    ...sseAdaptivePolling(connected, POLL_SLOW_MS),
  });
}

export function useConfigHistory(id: string) {
  return useQuery({
    queryKey: qk.configHistory.byDevice(id),
    queryFn: () => fetchConfigHistory(id),
    refetchInterval: false,
    staleTime: Infinity,
  });
}

export function useDeviceTestData(id: string) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.deviceTestData.byDevice(id),
    queryFn: () => getDeviceTestData(id),
    enabled: Boolean(id),
    ...sseAdaptivePolling(connected, POLL_SLOW_MS),
  });
}

export function useTestDataHistory(id: string) {
  return useQuery({
    queryKey: qk.testDataHistory.byDevice(id),
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
      queryClient.invalidateQueries({ queryKey: qk.deviceTestData.byDevice(id) });
      queryClient.invalidateQueries({ queryKey: qk.testDataHistory.byDevice(id) });
    },
  });
}

export function useDeviceLogs(id: string, lines = 200) {
  return useQuery({
    queryKey: qk.deviceLogs.byDevice(id, lines),
    queryFn: () => fetchDeviceLogs(id, lines),
    refetchInterval: POLL_FAST_MS,
    staleTime: 2_500,
  });
}

export function useDeviceCapabilities(deviceId: string) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.deviceCapabilities.byDevice(deviceId),
    queryFn: () => fetchDeviceCapabilities(deviceId),
    enabled: !!deviceId,
    ...sseAdaptivePolling(connected, POLL_SLOW_MS),
  });
}
