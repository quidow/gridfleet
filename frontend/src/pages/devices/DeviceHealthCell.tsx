import { memo } from 'react';
import type { DeviceRead } from '../../types';
import Popover from '../../components/ui/Popover';
import { deriveUnifiedHealth, type UnifiedHealthTone } from '../../lib/deviceUnifiedHealth';
import { formatBatteryLevel, formatChargingState } from '../../lib/hardwareTelemetry';

type Props = {
  device: DeviceRead;
};

const DOT_CLASSES: Record<UnifiedHealthTone, string> = {
  ok: 'bg-success-strong',
  warn: 'bg-warning-strong',
  error: 'bg-danger-strong',
  unknown: 'bg-neutral-strong',
};

const LABEL_CLASSES: Record<UnifiedHealthTone, string> = {
  ok: 'text-success-foreground',
  warn: 'text-warning-foreground',
  error: 'text-danger-foreground',
  unknown: 'text-text-2',
};

const REASON_CLASSES: Record<UnifiedHealthTone, string> = {
  ok: 'text-success-foreground',
  warn: 'text-warning-foreground',
  error: 'text-danger-foreground',
  unknown: 'text-text-2',
};

function displayReason(value: string): string {
  return value.split('|')[0]?.trim() || value;
}

function DeviceHealthCellInner({ device }: Props) {
  const health = deriveUnifiedHealth(device);
  const ariaLabel = health.summary ? `${health.label} — ${health.summary}` : health.label;
  const telemetry = device.hardware_telemetry_state;

  const hasTelemetryData =
    device.battery_level_percent !== null && device.battery_level_percent !== undefined
    || (device.charging_state !== null && device.charging_state !== undefined && device.charging_state !== 'unknown');
  const showTelemetry =
    health.tone !== 'error' && telemetry !== 'unsupported' && hasTelemetryData;
  const telemetryLine = `${formatBatteryLevel(device.battery_level_percent)} · ${formatChargingState(device.charging_state)}`;

  const showReasons = health.tone !== 'ok' && health.tone !== 'unknown' && health.reasons.length > 0;
  const showAutoManage = !device.auto_manage;

  const hasDetail = showTelemetry || showReasons || showAutoManage;

  const trigger = (
    <span className="inline-flex items-center gap-1.5">
      <span
        aria-hidden="true"
        className={`inline-block h-2 w-2 shrink-0 rounded-full ${DOT_CLASSES[health.tone]}`}
      />
      <span className={`text-xs font-medium ${LABEL_CLASSES[health.tone]}`}>{health.label}</span>
    </span>
  );

  if (!hasDetail) {
    return (
      <span className="inline-flex items-center gap-1.5" aria-label={ariaLabel}>
        {trigger}
      </span>
    );
  }

  const content = (
    <div className="space-y-2 text-xs leading-snug">
      {showReasons ? (
        <div>
          <p className="heading-label mb-0.5">{health.label}</p>
          <ul className={`space-y-0.5 ${REASON_CLASSES[health.tone]}`}>
            {health.reasons.map((reason) => (
              <li key={reason}>{displayReason(reason)}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {showTelemetry ? (
        <div>
          <p className="heading-label mb-0.5">Battery</p>
          <p className="text-text-2">{telemetryLine}</p>
        </div>
      ) : null}
      {showAutoManage ? (
        <p className="text-text-3">Auto-manage disabled</p>
      ) : null}
    </div>
  );

  return (
    <Popover
      ariaLabel={`Health details for ${device.name}`}
      trigger={trigger}
      triggerClassName="inline-flex items-center gap-1.5 rounded px-0.5 hover:bg-surface-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent-ring"
    >
      {content}
    </Popover>
  );
}

const DeviceHealthCell = memo(DeviceHealthCellInner);
export default DeviceHealthCell;
