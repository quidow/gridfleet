import { useQuery } from '@tanstack/react-query';
import {
  fetchSessionSummary,
  fetchDeviceUtilization,
  fetchDeviceReliability,
  fetchFleetOverview,
  fetchFleetCapacityTimeline,
} from '../api/analytics';
import type { AnalyticsParams, FleetCapacityTimelineParams, SessionSummaryParams } from '../api/analytics';

interface AnalyticsQueryOptions {
  enabled?: boolean;
}

export function useSessionSummary(params?: SessionSummaryParams, options?: AnalyticsQueryOptions) {
  return useQuery({
    queryKey: ['analytics', 'sessions-summary', params],
    queryFn: () => fetchSessionSummary(params),
    staleTime: 5 * 60_000,
    enabled: options?.enabled,
  });
}

export function useDeviceUtilization(params?: AnalyticsParams, options?: AnalyticsQueryOptions) {
  return useQuery({
    queryKey: ['analytics', 'device-utilization', params],
    queryFn: () => fetchDeviceUtilization(params),
    staleTime: 5 * 60_000,
    enabled: options?.enabled,
  });
}

export function useDeviceReliability(params?: AnalyticsParams, options?: AnalyticsQueryOptions) {
  return useQuery({
    queryKey: ['analytics', 'device-reliability', params],
    queryFn: () => fetchDeviceReliability(params),
    staleTime: 5 * 60_000,
    enabled: options?.enabled,
  });
}

export function useFleetOverview(params?: AnalyticsParams, options?: AnalyticsQueryOptions) {
  return useQuery({
    queryKey: ['analytics', 'fleet-overview', params],
    queryFn: () => fetchFleetOverview(params),
    staleTime: 5 * 60_000,
    enabled: options?.enabled,
  });
}

export function useFleetCapacityTimeline(
  params?: FleetCapacityTimelineParams,
  options?: AnalyticsQueryOptions,
) {
  return useQuery({
    queryKey: ['analytics', 'fleet-capacity-timeline', params],
    queryFn: () => fetchFleetCapacityTimeline(params),
    staleTime: 60_000,
    enabled: options?.enabled,
  });
}
