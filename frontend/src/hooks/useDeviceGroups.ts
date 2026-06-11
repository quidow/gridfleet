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
  groupUpdateTags,
  removeGroupMembers,
  updateDeviceGroup,
} from '../api/deviceGroups';
import type {
  BulkOperationResult,
  BulkMaintenanceEnter,
  BulkTagsUpdate,
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

export function useDeviceGroup(id: string) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.deviceGroup.detail(id),
    queryFn: () => fetchDeviceGroup(id),
    enabled: !!id,
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
    mutationFn: ({ id, data }: { id: string; data: DeviceGroupUpdate }) => updateDeviceGroup(id, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.deviceGroups.root });
      qc.invalidateQueries({ queryKey: qk.deviceGroup.root });
    },
  });
}

export function useDeleteDeviceGroup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteDeviceGroup(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.deviceGroups.root }),
  });
}

export function useAddGroupMembers() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ groupId, deviceIds }: { groupId: string; deviceIds: string[] }) =>
      addGroupMembers(groupId, deviceIds),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.deviceGroups.root });
      qc.invalidateQueries({ queryKey: qk.deviceGroup.root });
    },
  });
}

export function useRemoveGroupMembers() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ groupId, deviceIds }: { groupId: string; deviceIds: string[] }) =>
      removeGroupMembers(groupId, deviceIds),
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
  useGroupBulkMutation(({ groupId, body }: { groupId: string; body: BulkMaintenanceEnter }) =>
    groupEnterMaintenance(groupId, body));
export const useGroupUpdateTags = () =>
  useGroupBulkMutation(({ groupId, body }: { groupId: string; body: BulkTagsUpdate }) =>
    groupUpdateTags(groupId, body));
