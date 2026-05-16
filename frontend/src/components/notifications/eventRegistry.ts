import type { BadgeTone } from '../ui/Badge';

export type EventSeverity = BadgeTone;

export type FormattedEventDetails =
  | { kind: 'text'; text: string }
  | { kind: 'json'; text: string }
  | { kind: 'empty'; text: string };

type EventData = Record<string, unknown>;
type Renderer = (data: EventData) => string;

type RegistryEntry = {
  render: Renderer;
};

export type EventLike = {
  type: string;
  severity?: EventSeverity | null;
  data?: EventData | null;
};

export const SEEDED_EVENT_TYPES = [
  'run.completed',
  'run.failed',
  'run.cancelled',
  'host.offline',
  'host.online',
  'device.maintenance_start',
  'device.maintenance_end',
  'webhook.delivered',
  'webhook.failed',
  'config.updated',
  'session.stuck',
  'device.verified',
  'lifecycle.incident_open',
  'lifecycle.incident_resolved',
  'node.crash',
  'node.restart',
] as const;

function stringValue(value: unknown): string | null {
  return typeof value === 'string' && value.trim().length > 0 ? value.trim() : null;
}

function numberValue(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function firstString(data: EventData, keys: string[], fallback: string): string {
  for (const key of keys) {
    const value = stringValue(data[key]);
    if (value) return value;
  }
  return fallback;
}

function appendReason(data: EventData): string {
  const reason = stringValue(data.reason) ?? stringValue(data.error);
  return reason ? `: ${reason}` : '';
}

const LEGACY_FALLBACK: Record<string, EventSeverity> = {
  'device.operational_state_changed': 'warning',
  'device.hold_changed': 'warning',
  'device.hardware_health_changed': 'warning',
  'device.maintenance_start': 'info',
  'device.maintenance_end': 'success',
  'device.verified': 'success',
  'device.verification.updated': 'info',
  'node.crash': 'critical',
  'node.restart': 'info',
  'node.state_changed': 'info',
  'host.registered': 'success',
  'host.online': 'success',
  'host.offline': 'critical',
  'host.status_changed': 'warning',
  'host.heartbeat_lost': 'critical',
  'host.discovery_completed': 'info',
  'host.circuit_breaker.opened': 'critical',
  'host.circuit_breaker.closed': 'success',
  'session.started': 'info',
  'session.ended': 'info',
  'session.stuck': 'warning',
  'run.created': 'info',
  'run.active': 'info',
  'run.completed': 'success',
  'run.failed': 'critical',
  'run.cancelled': 'warning',
  'run.expired': 'critical',
  'lifecycle.incident_open': 'warning',
  'lifecycle.incident_resolved': 'success',
  'device_group.updated': 'info',
  'device_group.members_changed': 'warning',
  'bulk.operation_completed': 'warning',
  'settings.changed': 'neutral',
  'config.updated': 'neutral',
  'test_data.updated': 'neutral',
  'webhook.delivered': 'success',
  'webhook.failed': 'critical',
  'webhook.test': 'neutral',
  'system.cleanup_completed': 'neutral',
};

export function legacyFallbackSeverity(eventType: string): EventSeverity | null {
  return LEGACY_FALLBACK[eventType] ?? null;
}

export function resolveEventSeverity(event: EventLike): EventSeverity {
  return event.severity ?? legacyFallbackSeverity(event.type) ?? 'neutral';
}

const REGISTRY: Record<string, RegistryEntry> = {
  'device.operational_state_changed': {
    render: (data) => {
      const device = firstString(data, ['device_name', 'name'], 'Device');
      const oldStatus = firstString(data, ['old_operational_state'], 'unknown');
      const newStatus = firstString(data, ['new_operational_state'], 'unknown');
      return `${device}: operational state ${oldStatus} -> ${newStatus}${appendReason(data)}`;
    },
  },
  'device.hold_changed': {
    render: (data) => {
      const device = firstString(data, ['device_name', 'name'], 'Device');
      const oldHold = firstString(data, ['old_hold'], 'none');
      const newHold = firstString(data, ['new_hold'], 'none');
      return `${device}: hold ${oldHold} -> ${newHold}${appendReason(data)}`;
    },
  },
  'device.hardware_health_changed': {
    render: (data) => {
      const device = firstString(data, ['device_name', 'name'], 'Device');
      return `${device}: hardware ${firstString(data, ['old_status'], 'unknown')} -> ${firstString(data, ['new_status'], 'unknown')}`;
    },
  },
  'device.maintenance_start': {
    render: (data) => `${firstString(data, ['device_name', 'name'], 'Device')} entered maintenance`,
  },
  'device.maintenance_end': {
    render: (data) => `${firstString(data, ['device_name', 'name'], 'Device')} exited maintenance`,
  },
  'device.verified': {
    render: (data) => `${firstString(data, ['device_name', 'name'], 'Device')} verified`,
  },
  'device.verification.updated': {
    render: (data) => `Verification ${firstString(data, ['job_id'], 'job')}: ${firstString(data, ['status'], 'updated')}`,
  },
  'node.crash': {
    render: (data) => `Appium node for ${firstString(data, ['device_name', 'name'], 'device')} crashed${appendReason(data)}`,
  },
  'node.restart': {
    render: (data) => `Appium node for ${firstString(data, ['device_name', 'name'], 'device')} restarted`,
  },
  'node.state_changed': {
    render: (data) => {
      const device = firstString(data, ['device_name', 'name'], 'Device');
      const port = numberValue(data.port);
      return `${device}: node ${firstString(data, ['old_state'], 'unknown')} -> ${firstString(data, ['new_state'], 'unknown')}${port ? ` (port ${port})` : ''}`;
    },
  },
  'host.registered': {
    render: (data) => `Host registered: ${firstString(data, ['hostname', 'host', 'name'], 'host')}`,
  },
  'host.online': {
    render: (data) => `${firstString(data, ['hostname', 'host', 'name'], 'Host')} came online`,
  },
  'host.offline': {
    render: (data) => `${firstString(data, ['hostname', 'host', 'name'], 'Host')} went offline`,
  },
  'host.status_changed': {
    render: (data) => {
      const host = firstString(data, ['hostname', 'host', 'name'], 'Host');
      return `${host}: ${firstString(data, ['old_status'], 'unknown')} -> ${firstString(data, ['new_status'], 'unknown')}`;
    },
  },
  'host.heartbeat_lost': {
    render: (data) => `${firstString(data, ['hostname', 'host', 'name'], 'Host')}: ${numberValue(data.missed_count) ?? 0} missed heartbeats`,
  },
  'host.discovery_completed': {
    render: (data) => `${firstString(data, ['hostname', 'host', 'name'], 'Host')}: discovery completed`,
  },
  'host.circuit_breaker.opened': {
    render: (data) => {
      const host = firstString(data, ['hostname', 'host', 'name'], 'host');
      const failures = numberValue(data.consecutive_failures);
      const cooldown = numberValue(data.cooldown_seconds);
      const parts = [`Circuit breaker opened on ${host}`];
      if (failures !== null) parts.push(`after ${failures} consecutive failure(s)`);
      if (cooldown !== null) parts.push(`(cooldown ${cooldown}s)`);
      return parts.join(' ');
    },
  },
  'host.circuit_breaker.closed': {
    render: (data) => `Circuit breaker closed on ${firstString(data, ['hostname', 'host', 'name'], 'host')}`,
  },
  'session.started': {
    render: (data) => `${firstString(data, ['device_name', 'name'], 'Device')}: session started${stringValue(data.test_name) ? ` (${stringValue(data.test_name)})` : ''}`,
  },
  'session.ended': {
    render: (data) => `Session ended (${firstString(data, ['status'], 'unknown')})`,
  },
  'session.stuck': {
    render: (data) => `Session ${firstString(data, ['session_id'], 'unknown').slice(0, 8)} stuck on ${firstString(data, ['device_name', 'name'], 'device')}`,
  },
  'run.created': {
    render: (data) => `${firstString(data, ['name'], 'Run')} created`,
  },
  'run.active': {
    render: (data) => `${firstString(data, ['name'], 'Run')} active`,
  },
  'run.completed': {
    render: (data) => `${firstString(data, ['name'], 'Run')} completed`,
  },
  'run.failed': {
    render: (data) => `${firstString(data, ['name'], 'Run')} failed${appendReason(data)}`,
  },
  'run.cancelled': {
    render: (data) => `${firstString(data, ['name'], 'Run')} cancelled${appendReason(data)}`,
  },
  'run.expired': {
    render: (data) => `${firstString(data, ['name'], 'Run')} expired${appendReason(data)}`,
  },
  'lifecycle.incident_open': {
    render: (data) => `Incident opened: ${firstString(data, ['device_name', 'name'], 'device')}${appendReason(data)}`,
  },
  'lifecycle.incident_resolved': {
    render: (data) => `Incident resolved: ${firstString(data, ['device_name', 'name'], 'device')}`,
  },
  'device_group.updated': {
    render: (data) => `Device group ${firstString(data, ['group_id'], 'group')}: ${firstString(data, ['action'], 'updated')}`,
  },
  'device_group.members_changed': {
    render: (data) => {
      const group = firstString(data, ['group_id'], 'group');
      const added = numberValue(data.added);
      const removed = numberValue(data.removed);
      if (added !== null) return `Device group ${group}: added ${added} member(s)`;
      if (removed !== null) return `Device group ${group}: removed ${removed} member(s)`;
      return `Device group ${group}: membership changed`;
    },
  },
  'bulk.operation_completed': {
    render: (data) => `Bulk ${firstString(data, ['operation'], 'operation')}: ${numberValue(data.succeeded) ?? 0}/${numberValue(data.total) ?? 0} succeeded`,
  },
  'settings.changed': {
    render: (data) => {
      if (Array.isArray(data.keys)) return `Settings updated: ${data.keys.length} key(s)`;
      if (data.reset_all === true) return 'All settings reset to defaults';
      if (data.reset === true) return `Setting reset: ${firstString(data, ['key'], 'unknown')}`;
      return `Setting updated: ${firstString(data, ['key'], 'unknown')}`;
    },
  },
  'config.updated': {
    render: (data) => `${firstString(data, ['name', 'config_name'], 'Config')} updated${stringValue(data.changed_by) ? ` by ${stringValue(data.changed_by)}` : ''}`,
  },
  'test_data.updated': {
    render: (data) => `${firstString(data, ['device_name'], 'Device')} test_data updated${stringValue(data.changed_by) ? ` by ${stringValue(data.changed_by)}` : ''}`,
  },
  'webhook.delivered': {
    render: (data) => `${firstString(data, ['webhook_name', 'name'], 'Webhook')} delivered`,
  },
  'webhook.failed': {
    render: (data) => `${firstString(data, ['webhook_name', 'name'], 'Webhook')} failed${appendReason(data)}`,
  },
  'webhook.test': {
    render: (data) => `${firstString(data, ['webhook_name', 'name'], 'Webhook')}: test event published`,
  },
  'system.cleanup_completed': {
    render: (data) => `Cleanup completed: ${numberValue(data.sessions_deleted) ?? 0} sessions, ${numberValue(data.audit_entries_deleted) ?? 0} audit logs, ${numberValue(data.device_events_deleted) ?? 0} device events`,
  },
};

export function formatEventDetails(type: string, data: EventData | null | undefined): FormattedEventDetails {
  const payload = data ?? {};
  const entry = REGISTRY[type];
  if (entry) {
    return { kind: 'text', text: entry.render(payload) };
  }
  if (Object.keys(payload).length === 0) {
    return { kind: 'empty', text: 'No details' };
  }
  return { kind: 'json', text: JSON.stringify(payload, null, 2) };
}

export const EVENT_SEVERITY_LABEL: Record<EventSeverity, string> = {
  info: 'Info',
  success: 'Success',
  warning: 'Warning',
  critical: 'Critical',
  neutral: 'Neutral',
};
