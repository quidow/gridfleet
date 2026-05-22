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

// Aggregates change on minute-scale buckets; matches prior staleTime budget.
const ANALYTICS_POLL_MS = 5 * 60_000;

export function useSessionSummary(params?: SessionSummaryParams, options?: AnalyticsQueryOptions) {
  return useQuery({
    queryKey: ['analytics', 'sessions-summary', params],
    queryFn: () => fetchSessionSummary(params),
    refetchInterval: ANALYTICS_POLL_MS,
    staleTime: ANALYTICS_POLL_MS / 2,
    enabled: options?.enabled,
  });
}

export function useDeviceUtilization(params?: AnalyticsParams, options?: AnalyticsQueryOptions) {
  return useQuery({
    queryKey: ['analytics', 'device-utilization', params],
    queryFn: () => fetchDeviceUtilization(params),
    refetchInterval: ANALYTICS_POLL_MS,
    staleTime: ANALYTICS_POLL_MS / 2,
    enabled: options?.enabled,
  });
}

export function useDeviceReliability(params?: AnalyticsParams, options?: AnalyticsQueryOptions) {
  return useQuery({
    queryKey: ['analytics', 'device-reliability', params],
    queryFn: () => fetchDeviceReliability(params),
    refetchInterval: ANALYTICS_POLL_MS,
    staleTime: ANALYTICS_POLL_MS / 2,
    enabled: options?.enabled,
  });
}

export function useFleetOverview(params?: AnalyticsParams, options?: AnalyticsQueryOptions) {
  return useQuery({
    queryKey: ['analytics', 'fleet-overview', params],
    queryFn: () => fetchFleetOverview(params),
    refetchInterval: ANALYTICS_POLL_MS,
    staleTime: ANALYTICS_POLL_MS / 2,
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
    refetchInterval: ANALYTICS_POLL_MS,
    staleTime: ANALYTICS_POLL_MS / 2,
    enabled: options?.enabled,
  });
}
