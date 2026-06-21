import { SummaryPill } from '../../components/ui/SummaryPill';
import type { GridRouterCounts } from '../../types/gridRouter';

export function RouterSummaryPills({ counts }: { counts: GridRouterCounts }) {
  return (
    <>
      <SummaryPill tone="neutral" label="registered" value={counts.registered} />
      <SummaryPill tone="ok" label="available" value={counts.available} />
      <SummaryPill tone={counts.busy > 0 ? 'warn' : 'neutral'} label="busy" value={counts.busy} />
      <SummaryPill tone={counts.offline > 0 ? 'error' : 'neutral'} label="offline" value={counts.offline} />
      <SummaryPill tone="neutral" label="running" value={counts.running} />
      <SummaryPill tone="neutral" label="verifying" value={counts.verifying} />
      <SummaryPill tone="neutral" label="maintenance" value={counts.maintenance} />
      <SummaryPill tone="ok" label="sessions" value={counts.active_sessions} />
      <SummaryPill tone={counts.queue_depth > 0 ? 'warn' : 'neutral'} label="queue" value={counts.queue_depth} />
    </>
  );
}
