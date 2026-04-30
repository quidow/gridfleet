import type { ReactNode } from 'react';
import type { DeviceDetail, DeviceRead } from '../../types';
import { resolvePlatformLabel } from '../../lib/labels';
import EmulatorStateBadge from '../../components/EmulatorStateBadge';
import ReservationPill from './ReservationPill';

export function buildDeviceDetailSubtitleNode(
  device: Pick<DeviceRead, 'platform_id' | 'platform_label' | 'os_version' | 'host_id' | 'emulator_state'>
    & Pick<DeviceDetail, 'reservation'>,
  hostLabel: string | null,
): ReactNode {
  const host = hostLabel && hostLabel.length > 0 ? hostLabel : device.host_id;
  const metaParts = [resolvePlatformLabel(device.platform_id, device.platform_label), device.os_version, host].filter(
    (part): part is string => typeof part === 'string' && part.length > 0,
  );

  return (
    <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1">
      <span>{metaParts.join(' · ')}</span>
      {device.emulator_state ? <EmulatorStateBadge state={device.emulator_state} /> : null}
      {device.reservation ? <ReservationPill reservation={device.reservation} /> : null}
    </span>
  );
}
