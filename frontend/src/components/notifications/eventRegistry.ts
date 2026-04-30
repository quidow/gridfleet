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
  severity: EventSeverity;
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

const REGISTRY: Record<string, RegistryEntry> = {
  'device.availability_changed': {
    render: (data) => {
      const device = firstString(data, ['device_name', 'name'], 'Device');
      const oldStatus = firstString(data, ['old_availability_status', 'old_status'], 'unknown');
      const newStatus = firstString(data, ['new_availability_status', 'new_status'], 'unknown');
      return `${device}: availability ${oldStatus} -> ${newStatus}${appendReason(data)}`;
    },
    severity: 'warning',
  },
  'device.hardware_health_changed': {
    render: (data) => {
      const device = firstString(data, ['device_name', 'name'], 'Device');
      return `${device}: hardware ${firstString(data, ['old_status'], 'unknown')} -> ${firstString(data, ['new_status'], 'unknown')}`;
    },
    severity: 'warning',
  },
  'device.maintenance_start': {
    render: (data) => `${firstString(data, ['device_name', 'name'], 'Device')} entered maintenance`,
    severity: 'info',
  },
  'device.maintenance_end': {
    render: (data) => `${firstString(data, ['device_name', 'name'], 'Device')} exited maintenance`,
    severity: 'success',
  },
  'device.verified': {
    render: (data) => `${firstString(data, ['device_name', 'name'], 'Device')} verified`,
    severity: 'success',
  },
  'device.verification.updated': {
    render: (data) => `Verification ${firstString(data, ['job_id'], 'job')}: ${firstString(data, ['status'], 'updated')}`,
    severity: 'info',
  },
  'node.crash': {
    render: (data) => `Appium node for ${firstString(data, ['device_name', 'name'], 'device')} crashed${appendReason(data)}`,
    severity: 'danger',
  },
  'node.restart': {
    render: (data) => `Appium node for ${firstString(data, ['device_name', 'name'], 'device')} restarted`,
    severity: 'info',
  },
  'node.state_changed': {
    render: (data) => {
      const device = firstString(data, ['device_name', 'name'], 'Device');
      const port = numberValue(data.port);
      return `${device}: node ${firstString(data, ['old_state'], 'unknown')} -> ${firstString(data, ['new_state'], 'unknown')}${port ? ` (port ${port})` : ''}`;
    },
    severity: 'info',
  },
  'host.registered': {
    render: (data) => `Host registered: ${firstString(data, ['hostname', 'host', 'name'], 'host')}`,
    severity: 'success',
  },
  'host.online': {
    render: (data) => `${firstString(data, ['hostname', 'host', 'name'], 'Host')} came online`,
    severity: 'success',
  },
  'host.offline': {
    render: (data) => `${firstString(data, ['hostname', 'host', 'name'], 'Host')} went offline`,
    severity: 'danger',
  },
  'host.status_changed': {
    render: (data) => {
      const host = firstString(data, ['hostname', 'host', 'name'], 'Host');
      return `${host}: ${firstString(data, ['old_status'], 'unknown')} -> ${firstString(data, ['new_status'], 'unknown')}`;
    },
    severity: 'warning',
  },
  'host.heartbeat_lost': {
    render: (data) => `${firstString(data, ['hostname', 'host', 'name'], 'Host')}: ${numberValue(data.missed_count) ?? 0} missed heartbeats`,
    severity: 'danger',
  },
  'host.discovery_completed': {
    render: (data) => `${firstString(data, ['hostname', 'host', 'name'], 'Host')}: discovery completed`,
    severity: 'info',
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
    severity: 'danger',
  },
  'host.circuit_breaker.closed': {
    render: (data) => `Circuit breaker closed on ${firstString(data, ['hostname', 'host', 'name'], 'host')}`,
    severity: 'success',
  },
  'session.started': {
    render: (data) => `${firstString(data, ['device_name', 'name'], 'Device')}: session started${stringValue(data.test_name) ? ` (${stringValue(data.test_name)})` : ''}`,
    severity: 'info',
  },
  'session.ended': {
    render: (data) => `Session ended (${firstString(data, ['status'], 'unknown')})`,
    severity: 'info',
  },
  'session.stuck': {
    render: (data) => `Session ${firstString(data, ['session_id'], 'unknown').slice(0, 8)} stuck on ${firstString(data, ['device_name', 'name'], 'device')}`,
    severity: 'warning',
  },
  'run.created': {
    render: (data) => `${firstString(data, ['name'], 'Run')} created`,
    severity: 'info',
  },
  'run.ready': {
    render: (data) => `${firstString(data, ['name'], 'Run')} ready`,
    severity: 'info',
  },
  'run.active': {
    render: (data) => `${firstString(data, ['name'], 'Run')} active`,
    severity: 'info',
  },
  'run.completed': {
    render: (data) => `${firstString(data, ['name'], 'Run')} completed`,
    severity: 'success',
  },
  'run.failed': {
    render: (data) => `${firstString(data, ['name'], 'Run')} failed${appendReason(data)}`,
    severity: 'danger',
  },
  'run.cancelled': {
    render: (data) => `${firstString(data, ['name'], 'Run')} cancelled${appendReason(data)}`,
    severity: 'warning',
  },
  'run.expired': {
    render: (data) => `${firstString(data, ['name'], 'Run')} expired${appendReason(data)}`,
    severity: 'danger',
  },
  'lifecycle.incident_open': {
    render: (data) => `Incident opened: ${firstString(data, ['device_name', 'name'], 'device')}${appendReason(data)}`,
    severity: 'warning',
  },
  'lifecycle.incident_resolved': {
    render: (data) => `Incident resolved: ${firstString(data, ['device_name', 'name'], 'device')}`,
    severity: 'success',
  },
  'device_group.updated': {
    render: (data) => `Device group ${firstString(data, ['group_id'], 'group')}: ${firstString(data, ['action'], 'updated')}`,
    severity: 'info',
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
    severity: 'warning',
  },
  'bulk.operation_completed': {
    render: (data) => `Bulk ${firstString(data, ['operation'], 'operation')}: ${numberValue(data.succeeded) ?? 0}/${numberValue(data.total) ?? 0} succeeded`,
    severity: 'warning',
  },
  'settings.changed': {
    render: (data) => {
      if (Array.isArray(data.keys)) return `Settings updated: ${data.keys.length} key(s)`;
      if (data.reset_all === true) return 'All settings reset to defaults';
      if (data.reset === true) return `Setting reset: ${firstString(data, ['key'], 'unknown')}`;
      return `Setting updated: ${firstString(data, ['key'], 'unknown')}`;
    },
    severity: 'neutral',
  },
  'config.updated': {
    render: (data) => `${firstString(data, ['name', 'config_name'], 'Config')} updated${stringValue(data.changed_by) ? ` by ${stringValue(data.changed_by)}` : ''}`,
    severity: 'neutral',
  },
  'webhook.delivered': {
    render: (data) => `${firstString(data, ['webhook_name', 'name'], 'Webhook')} delivered`,
    severity: 'success',
  },
  'webhook.failed': {
    render: (data) => `${firstString(data, ['webhook_name', 'name'], 'Webhook')} failed${appendReason(data)}`,
    severity: 'danger',
  },
  'webhook.test': {
    render: (data) => `${firstString(data, ['webhook_name', 'name'], 'Webhook')}: test event published`,
    severity: 'neutral',
  },
  'system.cleanup_completed': {
    render: (data) => `Cleanup completed: ${numberValue(data.sessions_deleted) ?? 0} sessions, ${numberValue(data.audit_entries_deleted) ?? 0} audit logs, ${numberValue(data.device_events_deleted) ?? 0} device events`,
    severity: 'neutral',
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

export function severityForEventType(type: string): EventSeverity {
  return REGISTRY[type]?.severity ?? 'neutral';
}

export const EVENT_SEVERITY_LABEL: Record<EventSeverity, string> = {
  info: 'Info',
  success: 'Success',
  warning: 'Warning',
  danger: 'Critical',
  neutral: 'Neutral',
};
