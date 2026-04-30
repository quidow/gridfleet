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

function toChartDatum(point: FleetCapacityTimelinePoint): FleetCapacityChartDatum {
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

function gapDatum(timestampMs: number): FleetCapacityChartDatum {
  const timestamp = new Date(timestampMs).toISOString();
  return {
    timestamp,
    label: formatDateTime(timestamp),
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

  const bucketMs = timeline.bucket_minutes * 60_000;
  const rows: FleetCapacityChartDatum[] = [];
  timeline.series.forEach((point, index) => {
    if (index > 0) {
      const previous = timeline.series[index - 1];
      const previousMs = new Date(previous.timestamp).getTime();
      const currentMs = new Date(point.timestamp).getTime();
      if (currentMs - previousMs > bucketMs * 1.5) {
        rows.push(gapDatum(previousMs + bucketMs));
      }
    }
    rows.push(toChartDatum(point));
  });
  return rows;
}
