import api from './client';
import type {
  BulkAutoManageUpdate,
  BulkDeviceIds,
  BulkMaintenanceEnter,
  BulkOperationResult,
  BulkTagsUpdate,
} from '../types';

const BASE = '/devices/bulk';

export const bulkStartNodes = (body: BulkDeviceIds) =>
  api.post<BulkOperationResult>(`${BASE}/start-nodes`, body).then(r => r.data);

export const bulkStopNodes = (body: BulkDeviceIds) =>
  api.post<BulkOperationResult>(`${BASE}/stop-nodes`, body).then(r => r.data);

export const bulkRestartNodes = (body: BulkDeviceIds) =>
  api.post<BulkOperationResult>(`${BASE}/restart-nodes`, body).then(r => r.data);

export const bulkSetAutoManage = (body: BulkAutoManageUpdate) =>
  api.post<BulkOperationResult>(`${BASE}/set-auto-manage`, body).then(r => r.data);

export const bulkUpdateTags = (body: BulkTagsUpdate) =>
  api.post<BulkOperationResult>(`${BASE}/update-tags`, body).then(r => r.data);

export const bulkDelete = (body: BulkDeviceIds) =>
  api.post<BulkOperationResult>(`${BASE}/delete`, body).then(r => r.data);

export const bulkEnterMaintenance = (body: BulkMaintenanceEnter) =>
  api.post<BulkOperationResult>(`${BASE}/enter-maintenance`, body).then(r => r.data);

export const bulkExitMaintenance = (body: BulkDeviceIds) =>
  api.post<BulkOperationResult>(`${BASE}/exit-maintenance`, body).then(r => r.data);

export const bulkReconnect = (body: BulkDeviceIds) =>
  api.post<BulkOperationResult>(`${BASE}/reconnect`, body).then(r => r.data);
