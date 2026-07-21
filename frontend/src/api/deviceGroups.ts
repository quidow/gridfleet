import api from './client';
import type {
  BulkDeviceIds,
  BulkOperationResult,
  DeviceGroupCreate,
  DeviceGroupDetail,
  DeviceGroupMutationRead,
  DeviceGroupRead,
  DeviceGroupUpdate,
} from '../types';

export const fetchDeviceGroups = () =>
  api.get<DeviceGroupRead[]>('/device-groups').then(r => r.data);

export const fetchDeviceGroup = (key: string) =>
  api.get<DeviceGroupDetail>(`/device-groups/${encodeURIComponent(key)}`).then(r => r.data);

export const createDeviceGroup = (data: DeviceGroupCreate) =>
  api.post<DeviceGroupMutationRead>('/device-groups', data).then(r => r.data);

export const updateDeviceGroup = (key: string, data: DeviceGroupUpdate) =>
  api.patch<DeviceGroupMutationRead>(`/device-groups/${encodeURIComponent(key)}`, data).then(r => r.data);

export const deleteDeviceGroup = (key: string) =>
  api.delete(`/device-groups/${encodeURIComponent(key)}`);

export const addGroupMembers = (groupKey: string, deviceIds: string[]) =>
  api.post(`/device-groups/${encodeURIComponent(groupKey)}/members`, { device_ids: deviceIds }).then(r => r.data);

export const removeGroupMembers = (groupKey: string, deviceIds: string[]) =>
  api.delete(`/device-groups/${encodeURIComponent(groupKey)}/members`, { data: { device_ids: deviceIds } }).then(r => r.data);

export const groupStartNodes = (groupKey: string) =>
  api.post<BulkOperationResult>(`/device-groups/${encodeURIComponent(groupKey)}/bulk/start-nodes`).then(r => r.data);

export const groupStopNodes = (groupKey: string) =>
  api.post<BulkOperationResult>(`/device-groups/${encodeURIComponent(groupKey)}/bulk/stop-nodes`).then(r => r.data);

export const groupRestartNodes = (groupKey: string) =>
  api.post<BulkOperationResult>(`/device-groups/${encodeURIComponent(groupKey)}/bulk/restart-nodes`).then(r => r.data);

export const groupEnterMaintenance = (groupKey: string, body: BulkDeviceIds) =>
  api.post<BulkOperationResult>(`/device-groups/${encodeURIComponent(groupKey)}/bulk/enter-maintenance`, body).then(r => r.data);

export const groupExitMaintenance = (groupKey: string) =>
  api.post<BulkOperationResult>(`/device-groups/${encodeURIComponent(groupKey)}/bulk/exit-maintenance`).then(r => r.data);

export const groupReconnect = (groupKey: string) =>
  api.post<BulkOperationResult>(`/device-groups/${encodeURIComponent(groupKey)}/bulk/reconnect`).then(r => r.data);

export const groupDeleteDevices = (groupKey: string) =>
  api.post<BulkOperationResult>(`/device-groups/${encodeURIComponent(groupKey)}/bulk/delete`).then(r => r.data);
