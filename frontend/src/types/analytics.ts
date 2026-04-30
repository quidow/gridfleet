export interface SessionSummaryRow {
  group_key: string;
  total: number;
  passed: number;
  failed: number;
  error: number;
  avg_duration_sec: number | null;
}

export interface DeviceUtilizationRow {
  device_id: string;
  device_name: string;
  pack_id: string;
  platform_id: string;
  platform_label: string | null;
  total_session_time_sec: number;
  idle_time_sec: number;
  busy_pct: number;
  session_count: number;
}

export interface DeviceReliabilityRow {
  device_id: string;
  device_name: string;
  pack_id: string;
  platform_id: string;
  platform_label: string | null;
  health_check_failures: number;
  connectivity_losses: number;
  node_crashes: number;
  total_incidents: number;
}

export interface FleetDeviceSummary {
  device_id: string;
  device_name: string;
  pack_id: string;
  platform_id: string;
  platform_label: string | null;
  value: number;
}

export interface FleetOverview {
  devices_by_platform: Record<string, number>;
  avg_utilization_pct: number;
  most_used: FleetDeviceSummary[];
  least_used: FleetDeviceSummary[];
  most_reliable: FleetDeviceSummary[];
  least_reliable: FleetDeviceSummary[];
  pass_rate_pct: number | null;
  devices_needing_attention: number;
}

export interface FleetCapacityTimelinePoint {
  timestamp: string;
  total_capacity_slots: number;
  active_sessions: number;
  queued_requests: number;
  rejected_unfulfilled_sessions: number;
  available_capacity_slots: number;
  inferred_demand: number;
  hosts_total: number;
  hosts_online: number;
  devices_total: number;
  devices_available: number;
  devices_offline: number;
  devices_maintenance: number;
}

export interface FleetCapacityTimeline {
  date_from: string;
  date_to: string;
  bucket_minutes: number;
  series: FleetCapacityTimelinePoint[];
}
