import { Link } from 'react-router-dom';
import SummaryPill from '../../components/ui/SummaryPill';
import type { DeviceDetail } from '../../types';
import { getDeviceDetailStatusPills } from './deviceDetailSummary';

type Props = { device: DeviceDetail };

export default function DeviceDetailStatusPills({ device }: Props) {
  const pills = getDeviceDetailStatusPills(device);

  return (
    <>
      {pills.map((pill) => {
        const node = <SummaryPill tone={pill.tone} label={pill.label} value={pill.value} />;
        const ariaLabel = `${pill.label} ${pill.value}`;
        return pill.to ? (
          <Link
            key={pill.key}
            to={pill.to}
            title={pill.title}
            aria-label={ariaLabel}
            data-testid="device-detail-status-pill"
            className="rounded-full transition-transform hover:-translate-y-0.5 focus:outline-none focus:ring-2 focus:ring-accent focus:ring-offset-2 focus:ring-offset-surface-0"
          >
            {node}
          </Link>
        ) : (
          <span
            key={pill.key}
            title={pill.title}
            aria-label={ariaLabel}
            data-testid="device-detail-status-pill"
          >
            {node}
          </span>
        );
      })}
    </>
  );
}
