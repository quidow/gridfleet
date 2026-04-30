import { useState } from 'react';
import { useDevices } from '../../hooks/useDevices';
import { useGridStatus, useHealth } from '../../hooks/useGrid';
import { useHosts } from '../../hooks/useHosts';
import PageHeader from '../ui/PageHeader';
import SystemHealthPills from './SystemHealthPills';

export default function DashboardHeader() {
  const { dataUpdatedAt: healthUpdatedAt } = useHealth();
  const { dataUpdatedAt: gridUpdatedAt } = useGridStatus();
  const { dataUpdatedAt: hostsUpdatedAt } = useHosts();
  const { dataUpdatedAt: devicesUpdatedAt } = useDevices();
  const [renderedAt] = useState(() => Date.now());

  const lastUpdated =
    Math.max(healthUpdatedAt ?? 0, gridUpdatedAt ?? 0, hostsUpdatedAt ?? 0, devicesUpdatedAt ?? 0) || null;

  return (
    <PageHeader
      title="Dashboard"
      subtitle="Fleet overview"
      updatedAt={lastUpdated ?? renderedAt}
      summary={<SystemHealthPills />}
    />
  );
}
