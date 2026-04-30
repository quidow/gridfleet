import { useMemo } from 'react';
import { Link } from 'react-router-dom';
import { Activity, Server, Smartphone, type LucideIcon } from 'lucide-react';
import { useDevices } from '../../hooks/useDevices';
import { useHosts } from '../../hooks/useHosts';
import { useGridStatus, useHealth } from '../../hooks/useGrid';
import StatCard, { type StatCardTone } from '../ui/StatCard';
import { deriveDashboardFleetSummary, deriveSystemHealthSummary } from './dashboardSummary';

export default function StatCardsRow() {
  const { data: devices } = useDevices();
  const { data: hosts } = useHosts();
  const { data: grid } = useGridStatus();
  const { data: health } = useHealth();

  const fleet = useMemo(() => deriveDashboardFleetSummary(devices ?? []), [devices]);
  const system = useMemo(() => deriveSystemHealthSummary(health, grid, hosts), [health, grid, hosts]);
  const activeSessions = grid?.active_sessions ?? 0;
  const queueSize = grid?.queue_size ?? 0;

  const cards: {
    label: string;
    value: number;
    icon: LucideIcon;
    tone: StatCardTone;
    hint: string;
    to: string;
  }[] = [
    {
      label: 'Hosts',
      value: system.hostsTotal,
      icon: Server,
      tone: system.hostsOffline > 0 ? 'warn' : 'neutral',
      hint:
        system.hostsTotal === 0
          ? 'No hosts registered'
          : `${system.hostsOnline}/${system.hostsTotal} online`,
      to: '/hosts',
    },
    {
      label: 'Devices',
      value: fleet.total,
      icon: Smartphone,
      tone: fleet.offline > 0 ? 'warn' : 'neutral',
      hint: `${fleet.available} available · ${fleet.offline} offline`,
      to: '/devices',
    },
    {
      label: 'Sessions',
      value: activeSessions,
      icon: Activity,
      tone: activeSessions > 0 ? 'positive' : 'neutral',
      hint: `${activeSessions} running · ${queueSize} queued`,
      to: '/sessions',
    },
  ];

  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
      {cards.map(({ to, ...card }) => (
        <Link
          key={card.label}
          to={to}
          className="block rounded-lg focus:outline-none focus:ring-2 focus:ring-accent focus:ring-offset-2 focus:ring-offset-surface-0"
        >
          <StatCard {...card} />
        </Link>
      ))}
    </div>
  );
}
