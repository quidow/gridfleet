import type { ConnectionType, DeviceType, DriverPack, PlatformDescriptor, PlatformIconKind } from '../types';

export type PlatformSelectionKey = `${string}::${string}`;

export function makePlatformKey(packId: string, platformId: string): PlatformSelectionKey {
  return `${packId}::${platformId}`;
}

export function parsePlatformKey(key: string): { packId: string; platformId: string } | null {
  const parts = key.split('::');
  if (parts.length !== 2) return null;
  const [packId, platformId] = parts;
  if (!packId || !platformId) return null;
  return { packId, platformId };
}

export function platformDescriptorFromPack(
  pack: DriverPack,
  platformId: string,
): PlatformDescriptor | null {
  const platform = pack.platforms?.find((entry) => entry.id === platformId);
  if (!platform) return null;
  return {
    packId: pack.id,
    platformId,
    displayName: platform.display_name,
    appiumPlatformName: platform.appium_platform_name,
    iconKind: platform.display_metadata?.icon_kind ?? ('generic' satisfies PlatformIconKind),
    deviceTypes: platform.device_types as DeviceType[],
    connectionTypes: platform.connection_types as ConnectionType[],
    identityScheme: platform.identity_scheme,
    identityScope: platform.identity_scope,
    lifecycleActions: (platform.lifecycle_actions ?? []).map((action) => action.id),
    healthChecks: platform.health_checks ?? [],
    deviceFieldsSchema: platform.device_fields_schema,
    defaultCapabilities: platform.default_capabilities ?? {},
    connectionBehavior: platform.connection_behavior ?? {},
    deviceTypeOverrides: platform.device_type_overrides ?? {},
  };
}

export function platformDescriptorForDeviceType(
  descriptor: PlatformDescriptor | null,
  deviceType: DeviceType | null | undefined,
): PlatformDescriptor | null {
  if (!descriptor || !deviceType) return descriptor;
  const override = descriptor.deviceTypeOverrides?.[deviceType];
  if (!override) return descriptor;
  return {
    ...descriptor,
    identityScheme: override.identity?.scheme ?? descriptor.identityScheme,
    identityScope: override.identity?.scope ?? descriptor.identityScope,
    lifecycleActions: override.lifecycle_actions?.map((action) => action.id) ?? descriptor.lifecycleActions,
    deviceFieldsSchema: override.device_fields_schema ?? descriptor.deviceFieldsSchema,
    defaultCapabilities: override.default_capabilities ?? descriptor.defaultCapabilities,
    connectionBehavior: override.connection_behavior ?? descriptor.connectionBehavior,
  };
}

export function findPlatformDescriptor(
  packs: DriverPack[] | undefined,
  packId: string | null | undefined,
  platformId: string | null | undefined,
): PlatformDescriptor | null {
  if (!packs || !packId || !platformId) return null;
  const pack = packs.find((entry) => entry.id === packId);
  return pack ? platformDescriptorFromPack(pack, platformId) : null;
}

export function findPlatformDescriptorByKey(
  packs: DriverPack[] | undefined,
  key: string | null | undefined,
): PlatformDescriptor | null {
  if (!key) return null;
  const parsed = parsePlatformKey(key);
  return parsed ? findPlatformDescriptor(packs, parsed.packId, parsed.platformId) : null;
}
