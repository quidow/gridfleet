import type { components } from '../api/openapi';
import type { ConnectionType, DeviceType } from './shared';

type Schemas = components['schemas'];

export type PlatformIconKind = 'mobile' | 'tv' | 'set_top' | 'generic';

export type PlatformDeviceFieldDefault = string | number | boolean;

export type PlatformDeviceField = {
  id: string;
  label: string;
  type: 'string' | 'int' | 'bool' | 'path' | 'network_endpoint' | 'file_upload';
  required_for_session?: boolean;
  required_for_discovery?: boolean;
  sensitive?: boolean;
  required_for?: string[];
  default?: PlatformDeviceFieldDefault;
  capability_name?: string;
};

export type PlatformConnectionBehavior = {
  default_device_type?: DeviceType;
  default_connection_type?: ConnectionType;
  requires_ip_address?: boolean;
  requires_connection_target?: boolean;
  allow_transport_identity_until_host_resolution?: boolean;
  host_resolution_action?: string;
};

export type PlatformLifecycleAction = {
  id: string;
  label?: string;
};

export type PlatformHealthCheckLabel = {
  id: string;
  label: string;
};

export type PlatformDeviceTypeOverride = {
  identity?: {
    scheme?: string;
    scope?: 'global' | 'host';
  };
  lifecycle_actions?: PlatformLifecycleAction[];
  device_fields_schema?: PlatformDeviceField[];
  default_capabilities?: Record<string, unknown>;
  connection_behavior?: PlatformConnectionBehavior;
};

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

export type DriverPackPlatformsResponse = Omit<Schemas['PackPlatforms'], 'platforms'> & {
  platforms: DriverPackPlatform[];
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
export type ManifestWorkaround = Omit<Schemas['ManifestWorkaroundOut'], 'applies_when' | 'env'> & {
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
  | 'workarounds'
> & {
  state: PackState;
  appium_server?: AppiumInstallable | null;
  appium_driver?: AppiumInstallable | null;
  workarounds?: ManifestWorkaround[];
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

type RuntimePluginStatus = {
  name: string;
  version: string;
  status: string;
  blocked_reason: string | null;
};

export type HostRuntimeStatus = Omit<Schemas['HostRuntimeStatusOut'], 'plugins'> & {
  plugins: RuntimePluginStatus[];
};
export type HostPackDoctorStatus = Schemas['HostPackDoctorOut'];
export type HostPackFeatureStatus = Schemas['HostPackFeatureStatusOut'];
export type HostPackStatus = Schemas['HostPackStatusOut'];
export type HostDriverPacksStatus = Omit<Schemas['HostDriverPacksOut'], 'features' | 'runtimes'> & {
  runtimes: HostRuntimeStatus[];
  features: HostPackFeatureStatus[];
};

export type DriverPackHostDoctor = Schemas['DriverPackHostDoctorOut'];
export type DriverPackHostStatus = Omit<Schemas['DriverPackHostStatusOut'], 'doctor'> & {
  doctor: DriverPackHostDoctor[];
};
export type DriverPackHostsResponse = Omit<Schemas['DriverPackHostsOut'], 'hosts'> & {
  hosts: DriverPackHostStatus[];
};
