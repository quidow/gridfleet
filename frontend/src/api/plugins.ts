import api from './client';
import type {
  AppiumPlugin,
  AppiumPluginCreate,
  AppiumPluginUpdate,
  FleetPluginSyncResult,
  HostPluginStatus,
  PluginSyncResult,
} from '../types';

export async function fetchPlugins(): Promise<AppiumPlugin[]> {
  const { data } = await api.get('/plugins');
  return data;
}

export async function createPlugin(body: AppiumPluginCreate): Promise<AppiumPlugin> {
  const { data } = await api.post('/plugins', body);
  return data;
}

export async function updatePlugin(id: string, body: AppiumPluginUpdate): Promise<AppiumPlugin> {
  const { data } = await api.patch(`/plugins/${id}`, body);
  return data;
}

export async function deletePlugin(id: string): Promise<void> {
  await api.delete(`/plugins/${id}`);
}

export async function fetchHostPlugins(hostId: string): Promise<HostPluginStatus[]> {
  const { data } = await api.get(`/hosts/${hostId}/plugins`);
  return data;
}

export async function syncHostPlugins(hostId: string): Promise<PluginSyncResult> {
  const { data } = await api.post(`/hosts/${hostId}/plugins/sync`);
  return data;
}

export async function syncAllPlugins(): Promise<FleetPluginSyncResult> {
  const { data } = await api.post('/plugins/sync-all');
  return data;
}
