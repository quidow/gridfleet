import { SummaryPill } from '../../components/ui/SummaryPill';
import type { GridRouterCounts } from '../../types/gridRouter';

// The Router header answers the router's question — what can I route to, what's
// occupied, what's down — not the device lifecycle FSM. So the five operational states
// collapse into routing buckets: `open` is the allocator's eligible count (routable
// now); `not ready` is available-but-not-routable (cooldown / transitioning / reserved);
// `busy` and `down` merge the occupied / out-of-service states. Per-device reasons live
// on the node cards.
export function RouterSummaryPills({ counts }: { counts: GridRouterCounts }) {
  const notReady = counts.available - counts.eligible;
  const busy = counts.busy + counts.verifying;
  const down = counts.offline + counts.maintenance;
  return (
    <>
      <SummaryPill tone="ok" label="open" value={counts.eligible} />
      <SummaryPill tone={notReady > 0 ? 'warn' : 'neutral'} label="not ready" value={notReady} />
      <SummaryPill tone={busy > 0 ? 'warn' : 'neutral'} label="busy" value={busy} />
      <SummaryPill tone={down > 0 ? 'error' : 'neutral'} label="down" value={down} />
      <span className="h-4 w-px bg-border" aria-hidden />
      <SummaryPill tone="ok" label="sessions" value={counts.active_sessions} />
      <SummaryPill tone={counts.queue_depth > 0 ? 'warn' : 'neutral'} label="queue" value={counts.queue_depth} />
    </>
  );
}
