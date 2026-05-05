import type {
  DeviceEventType,
  DeviceLifecyclePolicySummaryState,
  DeviceRead,
  GridStatus,
  HealthStatus,
  HostRead,
  LifecycleIncidentRead,
  RunRead,
  RunState,
} from '../../types';
import type { BadgeTone } from '../ui/Badge';
import { isLifecycleSummaryActive } from '../lifecyclePolicy';
import { deviceChipStatus } from '../../lib/deviceState';
import type { DeviceChipStatus } from '../../types';

type GridHealthTone = 'ready' | 'warning' | 'error';

const ACTIVE_RUN_STATES: readonly RunState[] =['pending', 'preparing', 'ready', 'active', 'completing'];

interface DashboardFleetSummary {
  total: number;
  available: number;
  busy: number;
  offline: number;
  maintenance: number;
  reserved: number;
  needsAttention: number;
  hardwareWarning: number;
  hardwareCritical: number;
  staleTelemetry: number;
  lifecycleActive: number;
  lifecycleDevices: DeviceRead[];
  busyDevices: DeviceRead[];
  platformCounts: Record<string, number>;
}

interface GridHealth {
  tone: GridHealthTone;
  label: string;
  detail: string;
}

interface SystemHealthSummary {
  dbOk: boolean | null;
  gridHealth: GridHealth | null;
  hostsOnline: number;
  hostsTotal: number;
  hostsOffline: number;
  isDegraded: boolean;
}

interface GroupedLifecycleIncident {
  key: string;
  count: number;
  latestCreatedAt: string;
  deviceId: string;
  deviceName: string;
  summaryState: DeviceLifecyclePolicySummaryState;
  eventType: DeviceEventType;
  label: string;
  reason: string | null;
  detail: string | null;
  runName: string | null;
  backoffUntil: string | null;
}

function countByAvailability(devices: DeviceRead[], status: DeviceChipStatus) {
  return devices.filter((device) => deviceChipStatus(device) === status).length;
}

export function deriveDashboardFleetSummary(devices: DeviceRead[] = []): DashboardFleetSummary {
  const lifecycleDevices = devices.filter((device) => isLifecycleSummaryActive(device.lifecycle_policy_summary));
  const busyDevices = devices.filter((device) => device.operational_state === 'busy');
  const hardwareWarning = devices.filter((device) => device.hardware_health_status === 'warning').length;
  const hardwareCritical = devices.filter((device) => device.hardware_health_status === 'critical').length;
  const staleTelemetry = devices.filter((device) => device.hardware_telemetry_state === 'stale').length;
  const needsAttention = devices.filter((device) => device.needs_attention).length;

  const platformCounts: Record<string, number> = {};
  for (const device of devices) {
    platformCounts[device.platform_id] = (platformCounts[device.platform_id] ?? 0) + 1;
  }

  return {
    total: devices.length,
    available: countByAvailability(devices, 'available'),
    busy: countByAvailability(devices, 'busy'),
    offline: countByAvailability(devices, 'offline'),
    maintenance: countByAvailability(devices, 'maintenance'),
    reserved: countByAvailability(devices, 'reserved'),
    needsAttention,
    hardwareWarning,
    hardwareCritical,
    staleTelemetry,
    lifecycleActive: lifecycleDevices.length,
    lifecycleDevices,
    busyDevices,
    platformCounts,
  };
}

export function isActiveRun(run: Pick<RunRead, 'state'>): boolean {
  return ACTIVE_RUN_STATES.includes(run.state);
}

export function getGridHealth(status: GridStatus | null | undefined): GridHealth | null {
  if (!status) return null;

  const nodeCount = status.grid.value?.nodes?.length ?? 0;
  const registeredDevices = status.registry.device_count ?? 0;
  const ready = status.grid.value?.ready ?? status.grid.ready ?? false;
  const rawMessage = status.grid.value?.message ?? status.grid.message ?? null;
  const message = rawMessage === 'Selenium Grid not ready.' ? null : rawMessage;
  const error = status.grid.error ?? null;

  if (ready) return { tone: 'ready', label: 'Ready', detail: message ?? 'Accepting traffic' };
  if (error) return { tone: 'error', label: 'Unavailable', detail: error };
  if (nodeCount > 0) {
    return {
      tone: 'warning',
      label: 'Starting',
      detail: message ?? 'Waiting for nodes to finish registering',
    };
  }
  if (registeredDevices > 0) {
    return {
      tone: 'warning',
      label: 'Waiting for nodes',
      detail: 'No Appium nodes registered yet',
    };
  }
  return {
    tone: 'warning',
    label: 'Idle',
    detail: message ?? 'Waiting for devices to register',
  };
}

export function deriveSystemHealthSummary(
  health: HealthStatus | null | undefined,
  gridStatus: GridStatus | null | undefined,
  hosts: HostRead[] | null | undefined,
): SystemHealthSummary {
  const dbOk = health ? health.checks?.database === 'ok' : null;
  const gridHealth = getGridHealth(gridStatus);
  const hostsLoaded = hosts !== null && hosts !== undefined;
  const hostList = hosts ?? [];
  const hostsOnline = hostList.filter((host) => host.status === 'online').length;
  const hostsTotal = hostList.length;
  const hostsOffline = hostList.filter((host) => host.status === 'offline').length;

  return {
    dbOk,
    gridHealth,
    hostsOnline,
    hostsTotal,
    hostsOffline,
    isDegraded:
      dbOk === false
      || (gridHealth !== null && gridHealth.tone !== 'ready')
      || (hostsLoaded && hostsTotal > 0 && hostsOnline < hostsTotal)
      || (hostsLoaded && hostsTotal === 0),
  };
}

function incidentKey(incident: LifecycleIncidentRead) {
  return [incident.device_id, incident.summary_state, incident.label].join(':');
}

function timestamp(value: string) {
  const parsed = new Date(value).getTime();
  return Number.isFinite(parsed) ? parsed : 0;
}

export function groupLifecycleIncidents(incidents: LifecycleIncidentRead[] = []): GroupedLifecycleIncident[] {
  const grouped = new Map<string, GroupedLifecycleIncident>();

  for (const incident of incidents) {
    const key = incidentKey(incident);
    const current = grouped.get(key);

    if (!current) {
      grouped.set(key, {
        key,
        count: 1,
        latestCreatedAt: incident.created_at,
        deviceId: incident.device_id,
        deviceName: incident.device_name,
        summaryState: incident.summary_state,
        eventType: incident.event_type,
        label: incident.label,
        reason: incident.reason,
        detail: incident.detail,
        runName: incident.run_name,
        backoffUntil: incident.backoff_until,
      });
      continue;
    }

    current.count += 1;
    if (timestamp(incident.created_at) >= timestamp(current.latestCreatedAt)) {
      current.latestCreatedAt = incident.created_at;
      current.reason = incident.reason;
      current.detail = incident.detail;
      current.runName = incident.run_name;
      current.backoffUntil = incident.backoff_until;
    }
  }

  return [...grouped.values()].sort((a, b) => timestamp(b.latestCreatedAt) - timestamp(a.latestCreatedAt));
}

export function incidentToneFromEventType(eventType: DeviceEventType): BadgeTone {
  switch (eventType) {
    case 'node_crash':
    case 'lifecycle_recovery_failed':
    case 'lifecycle_run_excluded':
      return 'danger';
    case 'health_check_fail':
    case 'connectivity_lost':
    case 'hardware_health_changed':
    case 'lifecycle_deferred_stop':
    case 'lifecycle_auto_stopped':
    case 'lifecycle_recovery_suppressed':
    case 'lifecycle_recovery_backoff':
    case 'lifecycle_run_cooldown_set':
      return 'warning';
    case 'connectivity_restored':
    case 'lifecycle_recovered':
    case 'lifecycle_run_restored':
      return 'success';
    case 'node_restart':
      return 'info';
    default:
      return 'neutral';
  }
}
