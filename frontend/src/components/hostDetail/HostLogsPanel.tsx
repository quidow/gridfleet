import { SectionErrorBoundary } from '../ErrorBoundary';
import { Tabs, useTabParam } from '../ui';
import HostAgentLogPanel from './HostAgentLogPanel';
import HostEventsPanel from './HostEventsPanel';

interface Props {
  hostId: string;
}

const TABS = [
  { id: 'agent', label: 'Agent process' },
  { id: 'events', label: 'Host events' },
] as const;

const TAB_IDS = TABS.map((tab) => tab.id);

export default function HostLogsPanel({ hostId }: Props) {
  const [tab, setTab] = useTabParam('logs_tab', TAB_IDS as unknown as string[], 'agent');

  return (
    <section className="flex flex-col gap-4">
      <Tabs tabs={TABS as unknown as { id: string; label: string }[]} activeId={tab} onChange={setTab} />

      {tab === 'agent' && (
        <SectionErrorBoundary scope="host-agent-logs" resetKey={`${hostId}:agent`}>
          <HostAgentLogPanel hostId={hostId} />
        </SectionErrorBoundary>
      )}

      {tab === 'events' && (
        <SectionErrorBoundary scope="host-events" resetKey={`${hostId}:events`}>
          <HostEventsPanel hostId={hostId} />
        </SectionErrorBoundary>
      )}
    </section>
  );
}
