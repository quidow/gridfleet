import type { ReactNode } from 'react';
import type { DeviceRead } from '../../types';
import { resolvePlatformLabel } from '../../lib/labels';

export function buildDeviceDetailSubtitleNode(
  device: Pick<
    DeviceRead,
    'platform_id' | 'platform_label' | 'os_version' | 'os_version_display' | 'host_id'
  >,
  hostLabel: string | null,
): ReactNode {
  const host = hostLabel && hostLabel.length > 0 ? hostLabel : device.host_id;
  const osVersion = device.os_version_display ?? device.os_version;
  const metaParts = [resolvePlatformLabel(device.platform_id, device.platform_label), osVersion, host].filter(
    (part): part is string => typeof part === 'string' && part.length > 0,
  );

  return (
    <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1">
      <span>{metaParts.join(' · ')}</span>
    </span>
  );
}
