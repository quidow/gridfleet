import { useMemo } from 'react';
import { Link } from 'react-router-dom';
import { useHosts } from '../../hooks/useHosts';
import { useGridStatus, useHealth } from '../../hooks/useGrid';
import { useFleetOverview } from '../../hooks/useAnalytics';
import { useRolling7DayParams } from '../../hooks/useRolling7DayParams';
import { deriveSystemHealthSummary } from './dashboardSummary';

type CellTone = 'neutral' | 'warn' | 'critical';

const VALUE_TONE: Record<CellTone, string> = {
  neutral: 'text-text-1',
  warn: 'text-warning-foreground',
  critical: 'text-danger-foreground',
};

interface ScoreCell {
  key: string;
  label: string;
  value: string;
  hint: string;
  tone: CellTone;
  to: string;
}

export function Scorecard() {
  const { data: hosts } = useHosts();
  const { data: grid } = useGridStatus();
  const { data: health } = useHealth();
  const sevenDayParams = useRolling7DayParams();
  const overviewQuery = useFleetOverview(sevenDayParams);

  const system = useMemo(() => deriveSystemHealthSummary(health, grid, hosts), [health, grid, hosts]);

  const activeSessions = grid?.active_sessions ?? 0;
  const queueSize = grid?.queue_size ?? 0;
  const passRate = overviewQuery.data?.pass_rate_pct;
  const utilization = overviewQuery.data?.avg_utilization_pct;

  const cells: ScoreCell[] = [
    {
      key: 'hosts',
      label: 'Hosts',
      value: String(system.hostsTotal),
      hint:
        system.hostsTotal === 0
          ? 'None registered'
          : system.hostsOnline < system.hostsTotal
            ? `${system.hostsOnline} of ${system.hostsTotal} online`
            : `${system.hostsOnline} online`,
      tone: system.hostsTotal === 0 || system.hostsOffline > 0 ? 'warn' : 'neutral',
      to: '/hosts',
    },
    {
      key: 'sessions',
      label: 'Sessions',
      value: String(activeSessions),
      hint: `${queueSize} queued`,
      tone: activeSessions === 0 && queueSize > 0 ? 'warn' : 'neutral',
      to: '/sessions',
    },
    {
      key: 'pass-rate',
      label: 'Pass rate · 7d',
      value: passRate != null ? `${Math.round(passRate)}%` : '—',
      hint: passRate != null ? 'All sessions' : 'No runs',
      tone: 'neutral',
      to: '/analytics',
    },
    {
      key: 'utilization',
      label: 'Utilization · 7d',
      value: utilization != null ? `${Math.round(utilization)}%` : '—',
      hint: 'Fleet average',
      tone: 'neutral',
      to: '/analytics',
    },
  ];

  return (
    <div className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-border bg-border shadow-sm sm:grid-cols-4">
      {cells.map((cell) => (
        <Link
          key={cell.key}
          to={cell.to}
          className="bg-surface-1 px-4 py-3 transition-colors hover:bg-surface-2 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent"
        >
          <p className="heading-label">{cell.label}</p>
          <p className={`mt-1 text-2xl font-semibold tabular-nums ${VALUE_TONE[cell.tone]}`}>{cell.value}</p>
          <p className="mt-0.5 text-xs text-text-3">{cell.hint}</p>
        </Link>
      ))}
    </div>
  );
}
