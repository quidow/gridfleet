import { memo } from 'react';
import type { DeviceRead, HealthVerdictRead } from '../../types';
import { Popover } from '../../components/ui/Popover';
import { VERDICT_DOT_CLASSES, VERDICT_STATUS_LABELS, VERDICT_TEXT_CLASSES } from '../../lib/healthVerdicts';
import { formatDateTime } from '../../utils/dateFormatting';

type Props = { device: DeviceRead };

const SIGNALS = [
  { key: 'device', short: 'dev', label: 'Device' },
  { key: 'node', short: 'node', label: 'Node' },
  { key: 'viability', short: 'via', label: 'Viability' },
] as const;

function Dot({ short, label, verdict }: { short: string; label: string; verdict: HealthVerdictRead }) {
  const title = verdict.detail
    ? `${label}: ${VERDICT_STATUS_LABELS[verdict.status]} — ${verdict.detail}`
    : `${label}: ${VERDICT_STATUS_LABELS[verdict.status]}`;
  return (
    <span className="inline-flex items-center gap-1" aria-label={`${label} ${verdict.status}`} title={title}>
      <span
        aria-hidden="true"
        className={`inline-block h-2 w-2 shrink-0 rounded-full ${VERDICT_DOT_CLASSES[verdict.status]}`}
      />
      <span className="text-[10px] font-medium uppercase tracking-wide text-text-3">{short}</span>
    </span>
  );
}

function DeviceHealthCellInner({ device }: Props) {
  const hs = device.health_summary;

  const trigger = (
    <span className="inline-flex items-center gap-2">
      {SIGNALS.map(({ key, short, label }) => (
        <Dot key={key} short={short} label={label} verdict={hs[key]} />
      ))}
    </span>
  );

  return (
    <Popover
      ariaLabel={`Health details for ${device.name}`}
      trigger={trigger}
      triggerClassName="inline-flex items-center gap-2 rounded px-0.5 hover:bg-surface-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent-ring"
    >
      <div className="space-y-2 text-xs leading-snug">
        <div className="space-y-1">
          {SIGNALS.map(({ key, label }) => {
            const verdict = hs[key];
            return (
              <div key={key} className="flex items-baseline justify-between gap-3">
                <span className="heading-label">{label}</span>
                <span className={`text-right ${VERDICT_TEXT_CLASSES[verdict.status]}`}>
                  {VERDICT_STATUS_LABELS[verdict.status]}
                  {verdict.detail ? ` — ${verdict.detail}` : ''}
                  {verdict.checked_at ? (
                    <span className="block text-[10px] text-text-3">checked {formatDateTime(verdict.checked_at)}</span>
                  ) : null}
                </span>
              </div>
            );
          })}
        </div>
      </div>
    </Popover>
  );
}

export const DeviceHealthCell = memo(DeviceHealthCellInner);
