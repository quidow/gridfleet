import type { SettingRead } from '../../types';

export type SettingsSectionConfig = {
  id: string;
  title: string;
  description?: string;
  settingKeys: string[];
};

export type SettingsSectionGroup = SettingsSectionConfig & {
  settings: SettingRead[];
};

const SECTION_DEFINITIONS: Record<string, SettingsSectionConfig[]> = {
  general: [
    {
      id: 'heartbeat-health',
      title: 'Heartbeat & Host Health',
      description: 'How host liveness is derived from agent status pushes and verified by the reachability probe.',
      settingKeys: [
        'general.host_offline_after_sec',
      ],
    },
    {
      id: 'node-health',
      title: 'Node Health',
      description: 'Appium node health checks and restart thresholds.',
      settingKeys: ['general.node_max_failures'],
    },
    {
      id: 'session-management',
      title: 'Session Management',
      description: 'Grid queue timeouts and viability probes for idle devices.',
      settingKeys: [
        'general.session_viability_interval_sec',
        'general.session_viability_timeout_sec',
      ],
    },
    {
      id: 'recovery-lifecycle',
      title: 'Recovery / Lifecycle',
      description: 'Automatic recovery backoff behavior after repeated failures.',
      settingKeys: [
        'general.lifecycle_recovery_backoff_base_sec',
        'general.lifecycle_recovery_backoff_max_sec',
      ],
    },
  ],
  grid: [
    {
      id: 'appium-nodes',
      title: 'Appium Node Pool',
      description: 'Port allocation and startup timing.',
      settingKeys: [
        'appium.port_range_start',
        'appium.port_range_end',
        'appium.startup_timeout_sec',
        'appium.session_override',
      ],
    },
  ],
  notifications: [
    {
      id: 'toast-events',
      title: 'Toast Events',
      description: 'Choose which public events surface as operator toasts.',
      settingKeys: ['notifications.toast_events'],
    },
    {
      id: 'toast-delivery',
      title: 'Toast Delivery',
      description: 'Severity threshold and dismissal timing for notifications.',
      settingKeys: [
        'notifications.toast_severity_threshold',
        'notifications.toast_auto_dismiss_sec',
      ],
    },
  ],
  agent: [
    {
      id: 'agent-enrollment',
      title: 'Agent Enrollment',
      description: 'Version policy and host registration defaults.',
      settingKeys: ['agent.min_version', 'agent.auto_accept_hosts', 'agent.default_port'],
    },
  ],
  reservations: [
    {
      id: 'run-defaults',
      title: 'Run Defaults',
      description: 'Default reservation timing and timeout behavior for new runs.',
      settingKeys: [
        'reservations.default_ttl_minutes',
        'reservations.max_ttl_minutes',
        'reservations.default_heartbeat_timeout_sec',
      ],
    },
  ],
  device_checks: [
    {
      id: 'device-check-thresholds',
      title: 'Health-Check Thresholds',
      description: 'Consecutive-failure debounce and probe timing for device health checks.',
      settingKeys: [
        'device_checks.ip_ping.consecutive_fail_threshold',
        'device_checks.ip_ping.timeout_sec',
        'device_checks.ip_ping.count_per_cycle',
        'device_checks.probe_unanswered.consecutive_fail_threshold',
        'device_checks.probe_failed.consecutive_fail_threshold',
      ],
    },
  ],
  retention: [
    {
      id: 'retention-windows',
      title: 'Retention Windows',
      description: 'How long completed operational records remain available.',
      settingKeys: [
        'retention.sessions_days',
        'retention.audit_log_days',
        'retention.device_events_days',
        'retention.capacity_snapshots_days',
        'retention.host_resource_telemetry_hours',
      ],
    },
  ],
};

export function buildSettingsSections(category: string, settings: SettingRead[]): SettingsSectionGroup[] {
  const settingsByKey = new Map(settings.map((setting) => [setting.key, setting]));
  const sections = (SECTION_DEFINITIONS[category] ?? [])
    .map((section) => ({
      ...section,
      settings: section.settingKeys
        .map((key) => settingsByKey.get(key))
        .filter((setting): setting is SettingRead => !!setting),
    }))
    .filter((section) => section.settings.length > 0);

  const knownKeys = new Set(sections.flatMap((section) => section.settings.map((setting) => setting.key)));
  const remaining = settings.filter((setting) => !knownKeys.has(setting.key));

  if (remaining.length > 0) {
    sections.push({
      id: 'other',
      title: 'Other Settings',
      description: 'Additional settings in this category that are not part of a named section yet.',
      settingKeys: remaining.map((setting) => setting.key),
      settings: remaining,
    });
  }

  return sections;
}
