import type { FleetCapacityTimeline, FleetCapacityTimelinePoint } from '../types';
import { formatDateTime } from '../utils/dateFormatting';

type NullableMetric = number | null;

export interface FleetCapacityChartDatum {
  timestamp: string;
  label: string;
  isGap: boolean;
  total_capacity_slots: NullableMetric;
  active_sessions: NullableMetric;
  queued_requests: NullableMetric;
  rejected_unfulfilled_sessions: NullableMetric;
  available_capacity_slots: NullableMetric;
  inferred_demand: NullableMetric;
}

function realDatum(point: FleetCapacityTimelinePoint): FleetCapacityChartDatum {
  return {
    timestamp: point.timestamp,
    label: formatDateTime(point.timestamp),
    isGap: false,
    total_capacity_slots: point.total_capacity_slots,
    active_sessions: point.active_sessions,
    queued_requests: point.queued_requests,
    rejected_unfulfilled_sessions: point.rejected_unfulfilled_sessions,
    available_capacity_slots: point.available_capacity_slots,
    inferred_demand: point.inferred_demand,
  };
}

function gapDatum(point: FleetCapacityTimelinePoint): FleetCapacityChartDatum {
  return {
    timestamp: point.timestamp,
    label: formatDateTime(point.timestamp),
    isGap: true,
    total_capacity_slots: null,
    active_sessions: null,
    queued_requests: null,
    rejected_unfulfilled_sessions: null,
    available_capacity_slots: null,
    inferred_demand: null,
  };
}

export function buildFleetCapacityChartData(timeline: FleetCapacityTimeline | undefined): FleetCapacityChartDatum[] {
  if (!timeline?.series.length) return [];
  return timeline.series.map((point) => (point.has_data ? realDatum(point) : gapDatum(point)));
}
