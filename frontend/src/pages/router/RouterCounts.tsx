import { Activity, Boxes, CircleCheck, PowerOff } from 'lucide-react';

import { StatCard } from '../../components/ui/StatCard';
import { SummaryPill } from '../../components/ui/SummaryPill';
import type { GridRouterCounts } from '../../types/gridRouter';

export function RouterCounts({ counts }: { counts: GridRouterCounts }) {
  return (
    <div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatCard label="Registered" value={counts.registered} icon={Boxes} tone="neutral" />
        <StatCard label="Available" value={counts.available} icon={CircleCheck} tone="positive" />
        <StatCard label="Busy" value={counts.busy} icon={Activity} tone="warn" />
        <StatCard label="Offline" value={counts.offline} icon={PowerOff} tone="critical" />
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        <SummaryPill tone="neutral" label="running" value={counts.running} />
        <SummaryPill tone="neutral" label="verifying" value={counts.verifying} />
        <SummaryPill tone="neutral" label="maintenance" value={counts.maintenance} />
        <SummaryPill tone="ok" label="sessions" value={counts.active_sessions} />
        <SummaryPill tone={counts.queue_depth > 0 ? 'warn' : 'neutral'} label="queue" value={counts.queue_depth} />
      </div>
    </div>
  );
}
