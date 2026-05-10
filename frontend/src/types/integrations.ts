import type { components } from '../api/openapi';

type Schemas = components['schemas'];

export type WebhookRead = Schemas['WebhookRead'];
export type WebhookDeliveryRead = Omit<Schemas['WebhookDeliveryRead'], 'status'> & {
  status: 'pending' | 'delivered' | 'failed' | 'exhausted';
};
export type WebhookDeliveryListRead = Omit<Schemas['WebhookDeliveryListRead'], 'items'> & {
  items: WebhookDeliveryRead[];
};
export type WebhookCreate = Omit<Schemas['WebhookCreate'], 'enabled'> & {
  enabled?: boolean;
};
export type WebhookUpdate = Schemas['WebhookUpdate'];

export type AppiumPlugin = Schemas['PluginRead'];
export type AppiumPluginCreate = Omit<Schemas['PluginCreate'], 'enabled' | 'notes'> & {
  enabled?: boolean;
  notes?: string;
};
export type AppiumPluginUpdate = Schemas['PluginUpdate'];

export type HostPluginStatus = Omit<Schemas['HostPluginStatus'], 'status'> & {
  status: 'ok' | 'mismatch' | 'missing';
};
export type PluginSyncResult = Omit<Schemas['PluginSyncResult'], 'errors' | 'installed' | 'removed' | 'updated'> & {
  installed: string[];
  updated: string[];
  removed: string[];
  errors: Record<string, string>;
};
export type FleetPluginSyncResult = Omit<
  Schemas['FleetPluginSyncResult'],
  'failed_hosts' | 'online_hosts' | 'skipped_hosts' | 'synced_hosts'
> & {
  online_hosts: string[];
  synced_hosts: string[];
  failed_hosts: string[];
  skipped_hosts: string[];
};

export type LifecycleIncidentRead = Omit<
  Schemas['LifecycleIncidentRead'],
  'backoff_until' | 'detail' | 'reason' | 'run_id' | 'run_name' | 'source'
> & {
  backoff_until: string | null;
  detail: string | null;
  reason: string | null;
  run_id: string | null;
  run_name: string | null;
  source: string | null;
};

export interface SettingValidation {
  min?: number;
  max?: number;
  allowed_values?: string[];
  item_type?: 'string';
  item_allowed_values?: string[];
}

export type SettingRead = Omit<Schemas['SettingRead'], 'type' | 'validation'> & {
  type: 'int' | 'string' | 'bool' | 'json';
  validation?: SettingValidation | null;
};
export type SettingsGrouped = Omit<Schemas['SettingsGrouped'], 'settings'> & {
  settings: SettingRead[];
};
export type EventCatalogEntry = Schemas['EventCatalogEntryRead'];
