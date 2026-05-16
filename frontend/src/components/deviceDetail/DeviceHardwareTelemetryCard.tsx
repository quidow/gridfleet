import { HardwareHealthBadge } from '../HardwareHealthBadge';
import { HardwareTelemetryStateBadge } from '../HardwareTelemetryStateBadge';
import {
  formatBatteryLevel,
  formatBatteryTemperature,
  formatChargingState,
} from '../../lib/hardwareTelemetry';
import type { DeviceDetail } from '../../types';
import { formatDate } from './utils';
import DefinitionList from '../ui/DefinitionList';

type TelemetryItem = {
  term: string;
  definition: string;
};

type Props = {
  device: DeviceDetail;
};

export default function DeviceHardwareTelemetryCard({ device }: Props) {
  const hasBatteryData =
    device.battery_level_percent !== null ||
    device.charging_state !== null;
  const hasTemperatureData = device.battery_temperature_c !== null;
  const hasAnyData =
    hasBatteryData ||
    hasTemperatureData ||
    device.hardware_telemetry_reported_at !== null;
  const isUnsupported = device.hardware_telemetry_state === 'unsupported' && !hasBatteryData && !hasTemperatureData;
  const telemetryItems: TelemetryItem[] = [];
  if (hasBatteryData) {
    telemetryItems.push(
      { term: 'Battery Level', definition: formatBatteryLevel(device.battery_level_percent) },
      { term: 'Charging State', definition: formatChargingState(device.charging_state) },
    );
  }
  if (hasTemperatureData) {
    telemetryItems.push({ term: 'Temperature', definition: formatBatteryTemperature(device.battery_temperature_c) });
  }
  if (hasAnyData) {
    telemetryItems.push({ term: 'Last Reported', definition: formatDate(device.hardware_telemetry_reported_at) });
  }

  return (
    <div className="p-5">
      <div className="mb-4 flex items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-text-1">Hardware Telemetry</h2>
          <p className="mt-1 text-xs text-text-2">Latest device hardware snapshot and freshness.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <HardwareHealthBadge status={device.hardware_health_status} />
          <HardwareTelemetryStateBadge state={device.hardware_telemetry_state} />
        </div>
      </div>

      {isUnsupported ? (
        <p className="text-sm text-text-2">
          Hardware telemetry is not supported for this device.
        </p>
      ) : hasAnyData ? (
        <DefinitionList
          layout="justified"
          items={telemetryItems}
        />
      ) : (
        <p className="text-sm text-text-2">
          Not reported — the host agent has not submitted battery telemetry for this device.
        </p>
      )}
    </div>
  );
}
