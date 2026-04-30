import type { DeviceEventType, DeviceLifecyclePolicySummaryState } from './shared';

export interface WebhookRead {
  id: string;
  name: string;
  url: string;
  event_types: string[];
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface WebhookDeliveryRead {
  id: string;
  webhook_id: string;
  event_type: string;
  status: 'pending' | 'delivered' | 'failed' | 'exhausted';
  attempts: number;
  max_attempts: number;
  last_attempt_at: string | null;
  next_retry_at: string | null;
  last_error: string | null;
  last_http_status: number | null;
  created_at: string;
  updated_at: string;
}

export interface WebhookDeliveryListRead {
  items: WebhookDeliveryRead[];
  total: number;
}

export interface WebhookCreate {
  name: string;
  url: string;
  event_types: string[];
  enabled?: boolean;
}

export interface WebhookUpdate {
  name?: string;
  url?: string;
  event_types?: string[];
  enabled?: boolean;
}

export interface AppiumPlugin {
  id: string;
  name: string;
  version: string;
  source: string;
  package: string | null;
  enabled: boolean;
  notes: string;
  created_at: string;
  updated_at: string;
}

export interface AppiumPluginCreate {
  name: string;
  version: string;
  source: string;
  package?: string | null;
  enabled?: boolean;
  notes?: string;
}

export interface AppiumPluginUpdate {
  name?: string;
  version?: string;
  source?: string;
  package?: string | null;
  enabled?: boolean;
  notes?: string;
}

export interface HostPluginStatus {
  name: string;
  required_version: string;
  installed_version: string | null;
  status: 'ok' | 'mismatch' | 'missing';
  enabled: boolean;
}

export interface PluginSyncResult {
  installed: string[];
  updated: string[];
  removed: string[];
  errors: Record<string, string>;
}

export interface FleetPluginSyncResult {
  total_hosts: number;
  online_hosts: string[];
  synced_hosts: string[];
  failed_hosts: string[];
  skipped_hosts: string[];
}

export interface LifecycleIncidentRead {
  id: string;
  device_id: string;
  device_name: string;
  device_identity_value: string;
  pack_id: string;
  platform_id: string;
  platform_label: string | null;
  event_type: DeviceEventType;
  label: string;
  summary_state: DeviceLifecyclePolicySummaryState;
  reason: string | null;
  detail: string | null;
  source: string | null;
  run_id: string | null;
  run_name: string | null;
  backoff_until: string | null;
  created_at: string;
}

export interface SettingValidation {
  min?: number;
  max?: number;
  allowed_values?: string[];
  item_type?: 'string';
  item_allowed_values?: string[];
}

export interface SettingRead {
  key: string;
  value: unknown;
  default_value: unknown;
  is_overridden: boolean;
  category: string;
  description: string;
  type: 'int' | 'string' | 'bool' | 'json';
  validation: SettingValidation | null;
}

export interface SettingsGrouped {
  category: string;
  display_name: string;
  settings: SettingRead[];
}

export interface EventCatalogEntry {
  name: string;
  category: string;
  category_display_name: string;
  description: string;
  typical_data_fields: string[];
}
