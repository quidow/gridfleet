import { Link } from 'react-router-dom';
import SummaryPill, { type SummaryPillTone } from '../ui/SummaryPill';
import { useGridStatus, useHealth } from '../../hooks/useGrid';
import { useHosts } from '../../hooks/useHosts';
import { useEventStreamStatus } from '../../context/EventStreamContext';
import { deriveSystemHealthSummary } from './dashboardSummary';

type Pill = {
  key: string;
  label: string;
  value: string;
  tone: SummaryPillTone;
  title: string;
  to?: string;
};

function toneFromGridTone(tone: 'ready' | 'warning' | 'error' | undefined): SummaryPillTone {
  if (tone === 'ready') return 'ok';
  if (tone === 'error') return 'error';
  if (tone === 'warning') return 'warn';
  return 'neutral';
}

export default function SystemHealthPills() {
  const { connected } = useEventStreamStatus();
  const { data: grid } = useGridStatus();
  const { data: health } = useHealth();
  const { data: hosts } = useHosts();
  const system = deriveSystemHealthSummary(health, grid, hosts);
  const gridHealth = system.gridHealth;

  const pills: Pill[] = [
    {
      key: 'stream',
      label: 'Stream',
      value: connected ? 'Live' : 'Polling',
      tone: connected ? 'ok' : 'warn',
      title: connected ? 'Live updates streaming' : 'Falling back to poll',
      to: connected ? undefined : '/settings',
    },
    {
      key: 'db',
      label: 'DB',
      value: system.dbOk === null ? 'Unknown' : system.dbOk ? 'OK' : 'Down',
      tone: system.dbOk === null ? 'neutral' : system.dbOk ? 'ok' : 'error',
      title: system.dbOk === false ? 'Health check failing' : 'Backend persistence',
    },
    {
      key: 'grid',
      label: 'Grid',
      value: gridHealth?.label ?? 'Unknown',
      tone: gridHealth === null ? 'neutral' : toneFromGridTone(gridHealth.tone),
      title: gridHealth?.detail ?? 'Grid status not loaded',
      to: gridHealth && gridHealth.tone !== 'ready' ? '/sessions' : undefined,
    },
  ];

  return (
    <>
      {pills.map((pill) => {
        const pillNode = <SummaryPill tone={pill.tone} label={pill.label} value={pill.value} />;
        return pill.to ? (
          <Link
            key={pill.key}
            to={pill.to}
            title={pill.title}
            aria-label={`${pill.label} ${pill.value}`}
            data-testid="system-health-pill"
            className="rounded-full transition-transform hover:-translate-y-0.5 focus:outline-none focus:ring-2 focus:ring-accent focus:ring-offset-2 focus:ring-offset-surface-0"
          >
            {pillNode}
          </Link>
        ) : (
          <span
            key={pill.key}
            title={pill.title}
            aria-label={`${pill.label} ${pill.value}`}
            data-testid="system-health-pill"
          >
            {pillNode}
          </span>
        );
      })}
    </>
  );
}
