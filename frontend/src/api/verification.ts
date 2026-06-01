import api from './client';
import type {
  DeviceVerificationCreate,
  DeviceVerificationJob,
  DeviceVerificationUpdate,
} from '../types';

export async function startDeviceVerificationJob(body: DeviceVerificationCreate): Promise<DeviceVerificationJob> {
  const { data } = await api.post('/verification/jobs', body);
  return data;
}

export async function startExistingDeviceVerificationJob(
  id: string,
  body: DeviceVerificationUpdate,
): Promise<DeviceVerificationJob> {
  const { data } = await api.post(`/verification/devices/${id}/jobs`, body);
  return data;
}

export async function fetchDeviceVerificationJob(jobId: string): Promise<DeviceVerificationJob> {
  const { data } = await api.get(`/verification/jobs/${jobId}`);
  return data;
}
