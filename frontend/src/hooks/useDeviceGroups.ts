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

export function useDeviceGroups() {
  return useQuery({
    queryKey: ['device-groups'],
    queryFn: fetchDeviceGroups,
    refetchInterval: 30_000,
  });
}

export function useDeviceGroup(id: string) {
  return useQuery({
    queryKey: ['device-group', id],
    queryFn: () => fetchDeviceGroup(id),
    enabled: !!id,
    refetchInterval: 15_000,
  });
}

export function useCreateDeviceGroup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: DeviceGroupCreate) => createDeviceGroup(data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['device-groups'] }),
  });
}

export function useUpdateDeviceGroup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: { id: string; data: DeviceGroupUpdate }) => updateDeviceGroup(id, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['device-groups'] });
      qc.invalidateQueries({ queryKey: ['device-group'] });
    },
  });
}

export function useDeleteDeviceGroup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteDeviceGroup(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['device-groups'] }),
  });
}

export function useAddGroupMembers() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ groupId, deviceIds }: { groupId: string; deviceIds: string[] }) =>
      addGroupMembers(groupId, deviceIds),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['device-groups'] });
      qc.invalidateQueries({ queryKey: ['device-group'] });
    },
  });
}

export function useRemoveGroupMembers() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ groupId, deviceIds }: { groupId: string; deviceIds: string[] }) =>
      removeGroupMembers(groupId, deviceIds),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['device-groups'] });
      qc.invalidateQueries({ queryKey: ['device-group'] });
    },
  });
}

function useGroupBulkMutation<T>(mutationFn: (input: T) => Promise<BulkOperationResult>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['device-groups'] });
      qc.invalidateQueries({ queryKey: ['device-group'] });
      qc.invalidateQueries({ queryKey: ['devices'] });
      qc.invalidateQueries({ queryKey: ['device'] });
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
