import api from './client';
import type {
  BulkMaintenanceEnter,
  BulkOperationResult,
  BulkTagsUpdate,
  DeviceGroupCreate,
  DeviceGroupDetail,
  DeviceGroupRead,
  DeviceGroupUpdate,
} from '../types';

export const fetchDeviceGroups = () =>
  api.get<DeviceGroupRead[]>('/device-groups').then(r => r.data);

export const fetchDeviceGroup = (id: string) =>
  api.get<DeviceGroupDetail>(`/device-groups/${id}`).then(r => r.data);

export const createDeviceGroup = (data: DeviceGroupCreate) =>
  api.post<DeviceGroupRead>('/device-groups', data).then(r => r.data);

export const updateDeviceGroup = (id: string, data: DeviceGroupUpdate) =>
  api.patch<DeviceGroupRead>(`/device-groups/${id}`, data).then(r => r.data);

export const deleteDeviceGroup = (id: string) =>
  api.delete(`/device-groups/${id}`);

export const addGroupMembers = (groupId: string, deviceIds: string[]) =>
  api.post(`/device-groups/${groupId}/members`, { device_ids: deviceIds }).then(r => r.data);

export const removeGroupMembers = (groupId: string, deviceIds: string[]) =>
  api.delete(`/device-groups/${groupId}/members`, { data: { device_ids: deviceIds } }).then(r => r.data);

export const groupStartNodes = (groupId: string) =>
  api.post<BulkOperationResult>(`/device-groups/${groupId}/bulk/start-nodes`).then(r => r.data);

export const groupStopNodes = (groupId: string) =>
  api.post<BulkOperationResult>(`/device-groups/${groupId}/bulk/stop-nodes`).then(r => r.data);

export const groupRestartNodes = (groupId: string) =>
  api.post<BulkOperationResult>(`/device-groups/${groupId}/bulk/restart-nodes`).then(r => r.data);

export const groupEnterMaintenance = (groupId: string, body: BulkMaintenanceEnter) =>
  api.post<BulkOperationResult>(`/device-groups/${groupId}/bulk/enter-maintenance`, body).then(r => r.data);

export const groupExitMaintenance = (groupId: string) =>
  api.post<BulkOperationResult>(`/device-groups/${groupId}/bulk/exit-maintenance`).then(r => r.data);

export const groupReconnect = (groupId: string) =>
  api.post<BulkOperationResult>(`/device-groups/${groupId}/bulk/reconnect`).then(r => r.data);

export const groupUpdateTags = (groupId: string, body: BulkTagsUpdate) =>
  api.post<BulkOperationResult>(`/device-groups/${groupId}/bulk/update-tags`, body).then(r => r.data);

export const groupDeleteDevices = (groupId: string) =>
  api.post<BulkOperationResult>(`/device-groups/${groupId}/bulk/delete`).then(r => r.data);
