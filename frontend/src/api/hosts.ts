import api from './client';
import type { components } from './openapi';
import type {
  DiscoveryConfirm,
  DiscoveryConfirmResult,
  DiscoveryResult,
  HostCreate,
  HostDetail,
  HostResourceTelemetry,
  HostRead,
  HostDiagnostics,
  HostToolStatus,
  IntakeCandidate,
} from '../types';

export type AgentLogPage = components['schemas']['AgentLogPage'];
export type HostEventsPage = components['schemas']['HostEventsPage'];

export interface AgentLogQuery {
  level?: 'INFO' | 'WARNING' | 'ERROR';
  q?: string;
  since?: string;
  until?: string;
  limit?: number;
  offset?: number;
}

export interface HostEventsQuery {
  types?: string[];
  since?: string;
  until?: string;
  limit?: number;
  offset?: number;
}

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

export async function fetchHostAgentLogs(hostId: string, params: AgentLogQuery = {}): Promise<AgentLogPage> {
  const { data } = await api.get<AgentLogPage>(`/hosts/${hostId}/agent-logs`, { params });
  return data;
}

export async function fetchHostEvents(hostId: string, params: HostEventsQuery = {}): Promise<HostEventsPage> {
  const { types, ...rest } = params;
  const merged: Record<string, unknown> = { ...rest };
  if (types && types.length > 0) {
    merged.types = types.join(',');
  }
  const { data } = await api.get<HostEventsPage>(`/hosts/${hostId}/events`, { params: merged });
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
