import api from './client';
import type {
  AppiumNodeRead,
  ConnectionType,
  ConfigAuditEntry,
  DeviceDetail,
  DeviceHealth,
  HardwareHealthStatus,
  HardwareTelemetryState,
  DevicePatch,
  DeviceRead,
  SessionOutcomeHeatmapRow,
  DeviceChipStatus,
  DeviceType,
  DeviceVerificationCreate,
  DeviceVerificationJob,
  DeviceVerificationUpdate,
} from '../types';
import type { PaginatedResponse } from '../types/shared';

export async function fetchDevices(params?: {
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
}): Promise<DeviceRead[]> {
  const { data } = await api.get('/devices', { params });
  return data;
}

export type DeviceSortBy =
  | 'name'
  | 'platform'
  | 'device_type'
  | 'connection_type'
  | 'os_version'
  | 'host'
  | 'status'
  | 'created_at';
export type DeviceSortDir = 'asc' | 'desc';

export async function fetchDevicesPaginated(params: {
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
}): Promise<PaginatedResponse<DeviceRead>> {
  const { data } = await api.get('/devices', { params });
  return data;
}

export async function fetchDevice(id: string): Promise<DeviceDetail> {
  const { data } = await api.get(`/devices/${id}`);
  return data;
}

export async function fetchDeviceSessionOutcomeHeatmap(
  id: string,
  days = 90,
): Promise<SessionOutcomeHeatmapRow[]> {
  const { data } = await api.get(`/devices/${id}/session-outcome-heatmap`, { params: { days } });
  return data;
}

export async function startDeviceVerificationJob(body: DeviceVerificationCreate): Promise<DeviceVerificationJob> {
  const { data } = await api.post('/devices/verification-jobs', body);
  return data;
}

export async function startExistingDeviceVerificationJob(
  id: string,
  body: DeviceVerificationUpdate,
): Promise<DeviceVerificationJob> {
  const { data } = await api.post(`/devices/${id}/verification-jobs`, body);
  return data;
}

export async function fetchDeviceVerificationJob(jobId: string): Promise<DeviceVerificationJob> {
  const { data } = await api.get(`/devices/verification-jobs/${jobId}`);
  return data;
}

export async function updateDevice(id: string, body: DevicePatch): Promise<DeviceRead> {
  const { data } = await api.patch(`/devices/${id}`, body);
  return data;
}

export async function deleteDevice(id: string): Promise<void> {
  await api.delete(`/devices/${id}`);
}

export async function startNode(id: string): Promise<AppiumNodeRead> {
  const { data } = await api.post(`/devices/${id}/node/start`);
  return data;
}

export async function stopNode(id: string): Promise<AppiumNodeRead> {
  const { data } = await api.post(`/devices/${id}/node/stop`);
  return data;
}

export async function restartNode(id: string): Promise<AppiumNodeRead> {
  const { data } = await api.post(`/devices/${id}/node/restart`);
  return data;
}

export async function fetchDeviceConfig(id: string): Promise<Record<string, unknown>> {
  const { data } = await api.get(`/devices/${id}/config`);
  return data;
}


export async function fetchConfigHistory(id: string, limit = 50): Promise<ConfigAuditEntry[]> {
  const { data } = await api.get(`/devices/${id}/config/history`, { params: { limit } });
  return data;
}

export async function fetchDeviceHealth(id: string): Promise<DeviceHealth> {
  const { data } = await api.get(`/devices/${id}/health`);
  return data;
}

export async function runDeviceSessionTest(id: string): Promise<NonNullable<DeviceHealth['session_viability']>> {
  const { data } = await api.post(`/devices/${id}/session-test`);
  return data;
}

export async function reconnectDevice(id: string): Promise<{ success: boolean; identity_value: string; message: string }> {
  const { data } = await api.post(`/devices/${id}/reconnect`);
  return data;
}

export async function enterDeviceMaintenance(id: string, drain = false): Promise<DeviceRead> {
  const { data } = await api.post(`/devices/${id}/maintenance`, { drain });
  return data;
}

export async function exitDeviceMaintenance(id: string): Promise<DeviceRead> {
  const { data } = await api.post(`/devices/${id}/maintenance/exit`);
  return data;
}

export async function fetchDeviceLogs(id: string, lines = 100): Promise<{ lines: string[]; count: number }> {
  const { data } = await api.get(`/devices/${id}/logs`, { params: { lines } });
  return data;
}

export async function runDeviceLifecycleAction(
  id: string,
  action: string,
  args: Record<string, unknown> = {},
): Promise<{ success?: boolean; state?: string; detail?: string; [key: string]: unknown }> {
  const { data } = await api.post(`/devices/${id}/lifecycle/${action}`, args);
  return data;
}

export async function fetchDeviceCapabilities(deviceId: string): Promise<Record<string, unknown>> {
  const { data } = await api.get(`/devices/${deviceId}/capabilities`);
  return data;
}
