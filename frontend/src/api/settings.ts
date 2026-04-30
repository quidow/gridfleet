import api from './client';
import type { SettingRead, SettingsGrouped } from '../types';

export async function fetchSettings(): Promise<SettingsGrouped[]> {
  const { data } = await api.get('/settings');
  return data;
}

export async function bulkUpdateSettings(settings: Record<string, unknown>): Promise<SettingRead[]> {
  const { data } = await api.put('/settings/bulk', { settings });
  return data;
}

export async function resetSetting(key: string): Promise<SettingRead> {
  const { data } = await api.post(`/settings/reset/${key}`);
  return data;
}

export async function resetAllSettings(): Promise<{ status: string }> {
  const { data } = await api.post('/settings/reset-all');
  return data;
}
