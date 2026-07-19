import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  addGroupMembers,
  createDeviceGroup,
  deleteDeviceGroup,
  fetchDeviceGroup,
  fetchDeviceGroups,
  groupDeleteDevices,
  groupEnterMaintenance,
  groupExitMaintenance,
  groupReconnect,
  groupRestartNodes,
  groupStartNodes,
  groupStopNodes,
  removeGroupMembers,
  updateDeviceGroup,
} from '../api/deviceGroups';
import type {
  BulkDeviceIds,
  BulkOperationResult,
  DeviceGroupCreate,
  DeviceGroupUpdate,
} from '../types';
import { useEventStreamStatus } from '../context/EventStreamContext';
import { qk } from '../lib/queryKeys';
import { POLL_RELAXED_MS, POLL_SLOW_MS, sseAdaptivePolling } from './polling';

export function useDeviceGroups() {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.deviceGroups.root,
    queryFn: fetchDeviceGroups,
    ...sseAdaptivePolling(connected, POLL_SLOW_MS),
  });
}

export function useDeviceGroup(key: string) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.deviceGroup.detail(key),
    queryFn: () => fetchDeviceGroup(key),
    enabled: !!key,
    ...sseAdaptivePolling(connected, POLL_RELAXED_MS),
  });
}

export function useCreateDeviceGroup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: DeviceGroupCreate) => createDeviceGroup(data),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.deviceGroups.root }),
  });
}

export function useUpdateDeviceGroup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ key, data }: { key: string; data: DeviceGroupUpdate }) => updateDeviceGroup(key, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.deviceGroups.root });
      qc.invalidateQueries({ queryKey: qk.deviceGroup.root });
    },
  });
}

export function useDeleteDeviceGroup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (key: string) => deleteDeviceGroup(key),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.deviceGroups.root }),
  });
}

export function useAddGroupMembers() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ groupKey, deviceIds }: { groupKey: string; deviceIds: string[] }) =>
      addGroupMembers(groupKey, deviceIds),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.deviceGroups.root });
      qc.invalidateQueries({ queryKey: qk.deviceGroup.root });
    },
  });
}

export function useRemoveGroupMembers() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ groupKey, deviceIds }: { groupKey: string; deviceIds: string[] }) =>
      removeGroupMembers(groupKey, deviceIds),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.deviceGroups.root });
      qc.invalidateQueries({ queryKey: qk.deviceGroup.root });
    },
  });
}

function useGroupBulkMutation<T>(mutationFn: (input: T) => Promise<BulkOperationResult>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.deviceGroups.root });
      qc.invalidateQueries({ queryKey: qk.deviceGroup.root });
      qc.invalidateQueries({ queryKey: qk.devices.root });
      qc.invalidateQueries({ queryKey: qk.device.root });
    },
  });
}

export const useGroupStartNodes = () => useGroupBulkMutation(groupStartNodes);
export const useGroupStopNodes = () => useGroupBulkMutation(groupStopNodes);
export const useGroupRestartNodes = () => useGroupBulkMutation(groupRestartNodes);
export const useGroupReconnect = () => useGroupBulkMutation(groupReconnect);
export const useGroupExitMaintenance = () => useGroupBulkMutation(groupExitMaintenance);
export const useGroupDeleteDevices = () => useGroupBulkMutation(groupDeleteDevices);
export const useGroupEnterMaintenance = () =>
  useGroupBulkMutation(({ groupKey, body }: { groupKey: string; body: BulkDeviceIds }) =>
    groupEnterMaintenance(groupKey, body));
