import type { ConnectionType, DeviceType } from './shared';

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

export interface DriverPackPlatform {
  id: string;
  display_name: string;
  automation_name: string;
  appium_platform_name: string;
  device_types: string[];
  connection_types: string[];
  grid_slots: string[];
  identity_scheme: string;
  identity_scope: 'global' | 'host';
  discovery_kind: string;
  lifecycle_actions?: PlatformLifecycleAction[];
  health_checks?: PlatformHealthCheckLabel[];
  device_fields_schema: PlatformDeviceField[];
  capabilities: Record<string, unknown>;
  display_metadata?: { icon_kind?: PlatformIconKind };
  default_capabilities?: Record<string, unknown>;
  connection_behavior?: PlatformConnectionBehavior;
  device_type_overrides?: Record<string, PlatformDeviceTypeOverride>;
  parallel_resources?: {
    ports?: Array<{ capability_name: string; start: number }>;
    derived_data_path?: boolean;
  };
}

export interface DriverPackPlatformsResponse {
  pack_id: string;
  release: string;
  platforms: DriverPackPlatform[];
}

export type PackState = 'draft' | 'enabled' | 'draining' | 'disabled';

export interface PackFeatureAction {
  id: string;
  label: string;
}

export interface PackFeature {
  display_name: string;
  description_md: string;
  actions: PackFeatureAction[];
}

export interface FeatureActionResult {
  ok: boolean;
  detail: string;
  data: Record<string, unknown>;
}

export type RuntimePolicy =
  | { strategy: 'recommended'; appium_server_version?: null; appium_driver_version?: null }
  | { strategy: 'latest_patch'; appium_server_version?: null; appium_driver_version?: null }
  | { strategy: 'exact'; appium_server_version: string; appium_driver_version: string };

export interface AppiumInstallable {
  source: 'npm' | 'github' | 'local' | string;
  package: string;
  version: string;
  recommended?: string | null;
  known_bad: string[];
  github_repo?: string | null;
}

export interface ManifestWorkaround {
  id: string;
  applies_when: Record<string, unknown>;
  env: Record<string, string>;
}

export interface ManifestDoctorCheck {
  id: string;
  description: string;
  adapter_hook?: string | null;
}

export interface DriverPack {
  id: string;
  display_name: string;
  maintainer?: string;
  license?: string;
  state: PackState;
  current_release: string | null;
  appium_server?: AppiumInstallable | null;
  appium_driver?: AppiumInstallable | null;
  workarounds?: ManifestWorkaround[];
  doctor?: ManifestDoctorCheck[];
  insecure_features?: string[];
  features?: Record<string, PackFeature>;
  runtime_policy: RuntimePolicy;
  platforms?: DriverPackPlatform[];
  active_runs: number;
  live_sessions: number;
  derived_from?: { pack_id: string; release: string } | null;
  runtime_summary?: {
    installed_hosts: number;
    blocked_hosts: number;
    actual_appium_server_versions: string[];
    actual_appium_driver_versions: string[];
    driver_drift_hosts: number;
  };
}

export interface DriverPackRelease {
  release: string;
  is_current: boolean;
  artifact_sha256: string | null;
  created_at: string;
  platform_ids: string[];
}

export interface DriverPackReleasesResponse {
  pack_id: string;
  releases: DriverPackRelease[];
}

export interface HostRuntimeStatus {
  runtime_id: string;
  appium_server_package: string;
  appium_server_version: string;
  driver_specs: Array<Record<string, unknown>>;
  plugin_specs: Array<Record<string, unknown>>;
  appium_home: string | null;
  status: string;
  blocked_reason: string | null;
  plugins: Array<{
    name: string;
    version: string;
    status: string;
    blocked_reason: string | null;
  }>;
}

export interface HostPackDoctorStatus {
  pack_id: string;
  check_id: string;
  ok: boolean;
  message: string;
}

export interface HostPackFeatureStatus {
  pack_id: string;
  feature_id: string;
  ok: boolean;
  detail: string;
}

export interface HostPackStatus {
  pack_id: string;
  pack_release: string;
  runtime_id: string | null;
  status: string;
  resolved_install_spec: Record<string, unknown> | null;
  installer_log_excerpt: string | null;
  resolver_version: string | null;
  blocked_reason: string | null;
  installed_at: string | null;
  desired_appium_driver_version: string | null;
  installed_appium_driver_version: string | null;
  appium_driver_drift: boolean;
}

export interface HostDriverPacksStatus {
  host_id: string;
  packs: HostPackStatus[];
  runtimes: HostRuntimeStatus[];
  doctor: HostPackDoctorStatus[];
  features: HostPackFeatureStatus[];
}

export interface DriverPackHostDoctor {
  check_id: string;
  ok: boolean;
  message: string;
}

export interface DriverPackHostStatus {
  host_id: string;
  hostname: string;
  status: string;
  pack_release: string;
  runtime_id: string | null;
  pack_status: string;
  resolved_install_spec: Record<string, unknown> | null;
  installer_log_excerpt: string | null;
  resolver_version: string | null;
  blocked_reason: string | null;
  installed_at: string | null;
  desired_appium_driver_version: string | null;
  installed_appium_driver_version: string | null;
  appium_driver_drift: boolean;
  appium_home: string | null;
  runtime_status: string | null;
  runtime_blocked_reason: string | null;
  appium_server_version: string | null;
  doctor: DriverPackHostDoctor[];
}

export interface DriverPackHostsResponse {
  pack_id: string;
  hosts: DriverPackHostStatus[];
}
