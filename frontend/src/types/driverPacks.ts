import type { components } from '../api/openapi';
import type { ConnectionType, DeviceType } from './shared';

type Schemas = components['schemas'];

export type PlatformIconKind = 'mobile' | 'tv' | 'set_top' | 'generic';

// Re-exported from generated OpenAPI types (previously hand-typed).
export type PlatformDeviceField = Schemas['FieldSchemaOut'];
export type PlatformConnectionBehavior = Schemas['ConnectionBehaviorOut'];
export type PlatformLifecycleAction = Schemas['LifecycleActionOut'];
export type PlatformHealthCheckLabel = Schemas['HealthCheckLabelOut'];
export type PlatformDeviceTypeOverride = Schemas['PlatformDeviceTypeOverrideOut'];

export type PlatformDescriptor = {
  packId: string;
  platformId: string;
  displayName: string;
  appiumPlatformName: string;
  iconKind: PlatformIconKind;
  deviceTypes: DeviceType[];
  connectionTypes: ConnectionType[];
  identityScheme: string;
  identityScope: 'global' | 'host';
  lifecycleActions: string[];
  healthChecks: PlatformHealthCheckLabel[];
  deviceFieldsSchema: PlatformDeviceField[];
  defaultCapabilities: Record<string, unknown>;
  connectionBehavior: PlatformConnectionBehavior;
  deviceTypeOverrides: Record<string, PlatformDeviceTypeOverride>;
};

export type DriverPackPlatform = Omit<
  Schemas['PlatformOut'],
  | 'connection_behavior'
  | 'connection_types'
  | 'device_fields_schema'
  | 'device_type_overrides'
  | 'device_types'
  | 'display_metadata'
  | 'health_checks'
  | 'identity_scope'
  | 'lifecycle_actions'
  | 'parallel_resources'
> & {
  device_types: DeviceType[];
  connection_types: ConnectionType[];
  identity_scope: 'global' | 'host';
  lifecycle_actions?: PlatformLifecycleAction[];
  health_checks?: PlatformHealthCheckLabel[];
  device_fields_schema: PlatformDeviceField[];
  display_metadata?: { icon_kind?: PlatformIconKind };
  connection_behavior?: PlatformConnectionBehavior;
  device_type_overrides?: Record<string, PlatformDeviceTypeOverride>;
  parallel_resources?: {
    ports?: Array<{ capability_name: string; start: number }>;
    derived_data_path?: boolean;
  };
};

export type PackState = 'draft' | 'enabled' | 'draining' | 'disabled';

export type PackFeatureAction = Schemas['FeatureActionOut'];
export type PackFeature = Omit<Schemas['FeatureOut'], 'actions'> & {
  actions: PackFeatureAction[];
};
export type FeatureActionResult = Omit<Schemas['FeatureActionResultOut'], 'data'> & {
  data: Record<string, unknown>;
};

export type RuntimePolicy = Schemas['RuntimePolicy'];

export type AppiumInstallable = Omit<Schemas['AppiumInstallableOut'], 'known_bad'> & {
  known_bad: string[];
};
export type ManifestAppiumEnvRule = Omit<Schemas['ManifestAppiumEnvOut'], 'applies_when' | 'env'> & {
  applies_when: Record<string, unknown>;
  env: Record<string, string>;
};
export type ManifestDoctorCheck = Schemas['ManifestDoctorCheckOut'];

export type DriverPack = Omit<
  Schemas['PackOut'],
  | 'appium_driver'
  | 'appium_server'
  | 'doctor'
  | 'features'
  | 'platforms'
  | 'runtime_policy'
  | 'state'
  | 'appium_env'
> & {
  state: PackState;
  appium_server?: AppiumInstallable | null;
  appium_driver?: AppiumInstallable | null;
  appium_env?: ManifestAppiumEnvRule[];
  doctor?: ManifestDoctorCheck[];
  features?: Record<string, PackFeature>;
  runtime_policy: RuntimePolicy;
  platforms?: DriverPackPlatform[];
};

export type DriverPackRelease = Omit<Schemas['PackReleaseOut'], 'platform_ids'> & {
  platform_ids: string[];
};
export type DriverPackReleasesResponse = Omit<Schemas['PackReleasesOut'], 'releases'> & {
  releases: DriverPackRelease[];
};

export type HostPackDoctorStatus = Schemas['HostPackDoctorOut'];
export type HostPackFeatureStatus = Schemas['HostPackFeatureStatusOut'];
export type HostPackStatus = Schemas['HostPackStatusOut'];
export type HostDriverPacksStatus = Schemas['HostDriverPacksOut'];

export type DriverPackHostDoctor = Schemas['DriverPackHostDoctorOut'];
export type DriverPackHostStatus = Omit<Schemas['DriverPackHostStatusOut'], 'doctor'> & {
  doctor: DriverPackHostDoctor[];
};
export type DriverPackHostsResponse = Omit<Schemas['DriverPackHostsOut'], 'hosts'> & {
  hosts: DriverPackHostStatus[];
};
