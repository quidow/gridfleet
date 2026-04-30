import api from './client';
import type {
  DiscoveryConfirm,
  DiscoveryConfirmResult,
  DiscoveryResult,
  HostCreate,
  HostDetail,
  HostResourceTelemetry,
  HostRead,
  HostDiagnostics,
  HostToolEnsureJob,
  HostToolStatus,
  IntakeCandidate,
} from '../types';

export async function fetchHosts(): Promise<HostRead[]> {
  const { data } = await api.get('/hosts');
  return data;
}

export async function fetchHost(id: string): Promise<HostDetail> {
  const { data } = await api.get(`/hosts/${id}`);
  return data;
}

export async function fetchHostDiagnostics(id: string): Promise<HostDiagnostics> {
  const { data } = await api.get(`/hosts/${id}/diagnostics`);
  return data;
}

export async function fetchHostResourceTelemetry(
  id: string,
  params?: { sinceIso?: string; untilIso?: string; bucketMinutes?: number },
): Promise<HostResourceTelemetry> {
  const { data } = await api.get(`/hosts/${id}/resource-telemetry`, {
    params: {
      since: params?.sinceIso,
      until: params?.untilIso,
      bucket_minutes: params?.bucketMinutes,
    },
  });
  return data;
}

export async function fetchHostToolStatus(id: string): Promise<HostToolStatus> {
  const { data } = await api.get(`/hosts/${id}/tools/status`);
  return data;
}

export async function ensureHostTools(id: string): Promise<HostToolEnsureJob> {
  const { data } = await api.post(`/hosts/${id}/tools/ensure`);
  return data;
}

export async function fetchHostToolEnsureJob(hostId: string, jobId: string): Promise<HostToolEnsureJob> {
  const { data } = await api.get(`/hosts/${hostId}/tools/ensure-jobs/${jobId}`);
  return data;
}

export async function createHost(body: HostCreate): Promise<HostRead> {
  const { data } = await api.post('/hosts', body);
  return data;
}

export async function deleteHost(id: string): Promise<void> {
  await api.delete(`/hosts/${id}`);
}

export async function discoverDevices(hostId: string): Promise<DiscoveryResult> {
  const { data } = await api.post(`/hosts/${hostId}/discover`);
  return data;
}

export async function fetchIntakeCandidates(hostId: string): Promise<IntakeCandidate[]> {
  const { data } = await api.get(`/hosts/${hostId}/intake-candidates`);
  return data;
}

export async function confirmDiscovery(
  hostId: string,
  body: DiscoveryConfirm,
): Promise<DiscoveryConfirmResult> {
  const { data } = await api.post(`/hosts/${hostId}/discover/confirm`, body);
  return data;
}

export async function approveHost(id: string): Promise<HostRead> {
  const { data } = await api.post(`/hosts/${id}/approve`);
  return data;
}

export async function rejectHost(id: string): Promise<void> {
  await api.post(`/hosts/${id}/reject`);
}

export async function getHostCapabilities(): Promise<{ web_terminal_enabled: boolean }> {
  const { data } = await api.get('/hosts/capabilities');
  return data;
}
