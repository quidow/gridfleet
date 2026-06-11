import type { ConnectionType, DeviceType, DriverPack } from '../../types';
import { CONNECTION_TYPE_LABELS } from '../../lib/deviceWorkflow';
import { resolvePlatformLabel } from '../../lib/labels';
import { makePlatformKey } from '../../lib/platformSelection';

export type PlatformOption = {
  value: string;
  label: string;
  packId: string;
  platformId: string;
};

type PlatformOptionDraft = {
  value: string;
  baseLabel: string;
  packLabel: string;
  packId: string;
  platformId: string;
  deviceTypes: string[];
  connectionTypes: string[];
};

const PLATFORM_DEVICE_TYPE_QUALIFIER_LABELS: Record<DeviceType, string> = {
  real_device: 'Real Device',
  emulator: 'Emulator',
  simulator: 'Simulator',
};

export function buildPlatformOptions(catalog: DriverPack[]): PlatformOption[] {
  const drafts: PlatformOptionDraft[] = [];
  const baseLabelCounts = new Map<string, number>();
  for (const pack of catalog) {
    if (pack.state !== 'enabled') continue;
    for (const platform of pack.platforms ?? []) {
      const baseLabel = resolvePlatformLabel(platform.id, platform.display_name);
      baseLabelCounts.set(baseLabel, (baseLabelCounts.get(baseLabel) ?? 0) + 1);
      drafts.push({
        value: makePlatformKey(pack.id, platform.id),
        baseLabel,
        packLabel: pack.display_name,
        packId: pack.id,
        platformId: platform.id,
        deviceTypes: platform.device_types,
        connectionTypes: platform.connection_types,
      });
    }
  }
  const labels = drafts.map((draft) => platformOptionLabel(draft, (baseLabelCounts.get(draft.baseLabel) ?? 0) > 1));
  const labelCounts = new Map<string, number>();
  for (const label of labels) {
    labelCounts.set(label, (labelCounts.get(label) ?? 0) + 1);
  }
  return drafts.map((draft, index) => {
    const label = labels[index];
    return {
      value: draft.value,
      label: (labelCounts.get(label) ?? 0) > 1 ? `${label} - ${draft.packLabel}` : label,
      packId: draft.packId,
      platformId: draft.platformId,
    };
  });
}

function platformOptionLabel(draft: PlatformOptionDraft, needsQualifier: boolean): string {
  if (!needsQualifier) return draft.baseLabel;
  const qualifier = platformOptionQualifier(draft);
  return qualifier ? `${draft.baseLabel} - ${qualifier}` : draft.baseLabel;
}

function platformOptionQualifier({ platformId, deviceTypes, connectionTypes }: PlatformOptionDraft): string | null {
  const deviceTypeLabels = deviceTypes
    .map((deviceType) => PLATFORM_DEVICE_TYPE_QUALIFIER_LABELS[deviceType as DeviceType] ?? null)
    .filter((label): label is string => label !== null);
  if (deviceTypeLabels.length > 0) return deviceTypeLabels.join(' / ');

  const connectionTypeLabels = connectionTypes
    .map((connectionType) => CONNECTION_TYPE_LABELS[connectionType as ConnectionType] ?? null)
    .filter((label): label is string => label !== null);
  if (connectionTypeLabels.length > 0) return connectionTypeLabels.join(' / ');

  const suffixMatch = platformId.match(/(?:^|[_-])(real(?:[_-]device)?|emulator|simulator|network|usb|virtual)$/i);
  if (!suffixMatch) return null;
  return resolvePlatformLabel(suffixMatch[1].replace(/[_-]/g, ' '), null);
}
