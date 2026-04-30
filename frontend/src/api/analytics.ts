import api from './client';
import type {
  SessionSummaryRow,
  DeviceUtilizationRow,
  DeviceReliabilityRow,
  FleetCapacityTimeline,
  FleetOverview,
} from '../types';

export interface AnalyticsParams {
  date_from?: string;
  date_to?: string;
}

export interface SessionSummaryParams extends AnalyticsParams {
  group_by?: 'platform' | 'os_version' | 'device_id' | 'day';
}

export interface FleetCapacityTimelineParams extends AnalyticsParams {
  bucket_minutes?: number;
}

export async function fetchSessionSummary(params?: SessionSummaryParams): Promise<SessionSummaryRow[]> {
  const { data } = await api.get('/analytics/sessions/summary', { params });
  return data;
}

export async function fetchDeviceUtilization(params?: AnalyticsParams): Promise<DeviceUtilizationRow[]> {
  const { data } = await api.get('/analytics/devices/utilization', { params });
  return data;
}

export async function fetchDeviceReliability(params?: AnalyticsParams): Promise<DeviceReliabilityRow[]> {
  const { data } = await api.get('/analytics/devices/reliability', { params });
  return data;
}

export async function fetchFleetOverview(params?: AnalyticsParams): Promise<FleetOverview> {
  const { data } = await api.get('/analytics/fleet/overview', { params });
  return data;
}

export async function fetchFleetCapacityTimeline(
  params?: FleetCapacityTimelineParams,
): Promise<FleetCapacityTimeline> {
  const { data } = await api.get('/analytics/fleet/capacity-timeline', { params });
  return data;
}

export function downloadAnalyticsCsv(
  endpoint: 'sessions/summary' | 'devices/utilization' | 'devices/reliability',
  params?: AnalyticsParams & { group_by?: string },
): void {
  const searchParams = new URLSearchParams({ format: 'csv' });
  if (params?.date_from) searchParams.set('date_from', params.date_from);
  if (params?.date_to) searchParams.set('date_to', params.date_to);
  if (params?.group_by) searchParams.set('group_by', params.group_by);
  window.open(`/api/analytics/${endpoint}?${searchParams.toString()}`, '_blank');
}
