import { useDriverPackCatalog } from './useDriverPacks';
import type { DriverPack, PlatformDescriptor } from '../types';
import {
  findPlatformDescriptor as findPackPlatformDescriptor,
  platformDescriptorFromPack,
} from '../lib/platformSelection';

export {
  findPlatformDescriptor,
  findPlatformDescriptorByKey,
  makePlatformKey,
  platformDescriptorForDeviceType,
  parsePlatformKey,
} from '../lib/platformSelection';

function findFirstPlatformDescriptor(
  packs: DriverPack[] | undefined,
  platformId: string | null | undefined,
): PlatformDescriptor | null {
  if (!packs || !platformId) return null;
  for (const pack of packs) {
    const descriptor = platformDescriptorFromPack(pack, platformId);
    if (descriptor) return descriptor;
  }
  return null;
}

export function usePlatformDescriptor(
  packIdOrPlatformId: string | null | undefined,
  platformId?: string | null | undefined,
): PlatformDescriptor | null {
  const { data } = useDriverPackCatalog();
  if (platformId !== undefined) {
    return findPackPlatformDescriptor(data, packIdOrPlatformId, platformId);
  }
  return findFirstPlatformDescriptor(data, packIdOrPlatformId);
}
