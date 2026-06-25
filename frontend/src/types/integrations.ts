import type { components } from '../api/openapi';

type Schemas = components['schemas'];

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

export type SettingValidation = Schemas['SettingValidation'];

export type SettingRead = Omit<Schemas['SettingRead'], 'type' | 'validation'> & {
  type: 'int' | 'string' | 'bool' | 'json';
  validation?: SettingValidation | null;
};
export type SettingsGrouped = Omit<Schemas['SettingsGrouped'], 'settings'> & {
  settings: SettingRead[];
};
export type EventCatalogEntry = Schemas['EventCatalogEntryRead'];
