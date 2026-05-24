import { useParams, useSearchParams } from 'react-router-dom';
import { useApproveHost, useHost, useRejectHost } from '../hooks/useHosts';
import { LoadingSpinner } from '../components/LoadingSpinner';
import { SetupVerificationModal } from './devices/SetupVerificationModal';
import { HostDiscoveryModal } from '../components/hosts/HostDiscoveryModal';
import { useHostDiscoveryFlow } from '../components/hosts/useHostDiscoveryFlow';
import { getVerificationAction } from '../lib/deviceWorkflow';
import { usePageTitle } from '../hooks/usePageTitle';
import { PageHeader, Tabs, useTabParam } from '../components/ui';
import { SectionErrorBoundary } from '../components/ErrorBoundary';
import { HostDetailStatusPills } from './hostDetail/HostDetailStatusPills';
import { HostOverviewPanel } from '../components/hostDetail/HostOverviewPanel';
import { HostDevicesPanel } from '../components/hostDetail/HostDevicesPanel';
import { HostDriversPanel } from '../components/hostDetail/HostDriversPanel';
import { HostPluginsPanel } from '../components/hostDetail/HostPluginsPanel';
import { HostAgentLogPanel } from '../components/hostDetail/HostAgentLogPanel';
import { HostEventsPanel } from '../components/hostDetail/HostEventsPanel';
import { HostToolEnvPanel } from '../components/hostDetail/HostToolEnvPanel';
import type { HostDetail as HostDetailType } from '../types';
// HostDetail type alias avoids shadowing the default-exported component name

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'devices', label: 'Devices' },
  { id: 'drivers', label: 'Drivers' },
  { id: 'plugins', label: 'Plugins' },
  { id: 'environment', label: 'Environment' },
  { id: 'agent-logs', label: 'Agent Logs' },
  { id: 'events', label: 'Events' },
] as const;

const TAB_IDS = TABS.map((t) => t.id);

const LEGACY_TAB_MAP: Record<string, string> = {
  diagnostics: 'overview',
  logs: 'agent-logs',
};

export function HostDetail() {
  const { id } = useParams<{ id: string }>();
  const { data: host, isLoading, dataUpdatedAt } = useHost(id!);
  usePageTitle(host?.hostname ?? 'Host');
  const approveMut = useApproveHost();
  const rejectMut = useRejectHost();
  const discoveryFlow = useHostDiscoveryFlow(id ?? null);
  const [searchParams, setSearchParams] = useSearchParams();
  const legacyTab = searchParams.get('tab');
  if (legacyTab && legacyTab in LEGACY_TAB_MAP) {
    const next = new URLSearchParams(searchParams);
    next.set('tab', LEGACY_TAB_MAP[legacyTab]);
    next.delete('logs_tab');
    setSearchParams(next, { replace: true });
  }
  const [tab, setTab] = useTabParam('tab', TAB_IDS as unknown as string[], 'overview');

  if (isLoading) {
    return <LoadingSpinner />;
  }

  if (!host) return <p className="text-text-3 text-center mt-12">Host not found</p>;

  const hostOnline = host.status === 'online';
  const hostDetail = host as HostDetailType;

  return (
    <div>
      <PageHeader
        title={host.hostname}
        subtitle={`${host.os_type} · ${host.ip}:${host.agent_port}`}
        updatedAt={dataUpdatedAt || host.last_heartbeat}
        summary={<HostDetailStatusPills host={hostDetail} />}
      />

      <Tabs tabs={TABS as unknown as { id: string; label: string }[]} activeId={tab} onChange={setTab} className="mb-6" />

      <div className="fade-in-stagger flex flex-col gap-6">
      {tab === 'overview' && (
        <SectionErrorBoundary scope="host-overview">
          <HostOverviewPanel
            host={host}
            approvePending={approveMut.isPending}
            rejectPending={rejectMut.isPending}
            onApprove={() => approveMut.mutate(id!)}
            onReject={() => rejectMut.mutate(id!)}
          />
        </SectionErrorBoundary>
      )}

      {tab === 'devices' && (
        <SectionErrorBoundary scope="host-devices">
          <HostDevicesPanel
            host={hostDetail}
            onDiscover={() => discoveryFlow.handleDiscover()}
            discoverPending={discoveryFlow.discoverMut.isPending}
          />
        </SectionErrorBoundary>
      )}

      {tab === 'agent-logs' && (
        <SectionErrorBoundary scope="host-agent-logs">
          <HostAgentLogPanel hostId={id!} />
        </SectionErrorBoundary>
      )}

      {tab === 'events' && (
        <SectionErrorBoundary scope="host-events">
          <HostEventsPanel hostId={id!} />
        </SectionErrorBoundary>
      )}

      {tab === 'environment' && (
        <SectionErrorBoundary scope="host-environment">
          <HostToolEnvPanel hostId={id!} />
        </SectionErrorBoundary>
      )}

      {tab === 'drivers' && (
        <SectionErrorBoundary scope="host-drivers">
          <HostDriversPanel hostId={id!} hostOnline={hostOnline} />
        </SectionErrorBoundary>
      )}

      {tab === 'plugins' && (
        <SectionErrorBoundary scope="host-plugins">
          <HostPluginsPanel hostId={id!} />
        </SectionErrorBoundary>
      )}
      </div>

      <HostDiscoveryModal
        discoveryResult={discoveryFlow.discoveryResult}
        isPending={discoveryFlow.confirmMut.isPending}
        onClose={discoveryFlow.closeDiscovery}
        onConfirm={discoveryFlow.handleConfirm}
        onImportAndVerify={discoveryFlow.handleImportAndVerify}
        onToggleAdd={discoveryFlow.toggleAdd}
        onToggleRemove={discoveryFlow.toggleRemove}
        selectedAddIdentities={discoveryFlow.selectedAddIdentities}
        selectedRemoveIdentities={discoveryFlow.selectedRemoveIdentities}
        setSelectedAddIdentities={discoveryFlow.setSelectedAddIdentities}
        setSelectedRemoveIdentities={discoveryFlow.setSelectedRemoveIdentities}
      />

      {discoveryFlow.verifyDevice ? (
        <SetupVerificationModal
          isOpen={!!discoveryFlow.verifyDevice}
          onClose={() => discoveryFlow.setVerifyDevice(null)}
          existingDevice={discoveryFlow.verifyDevice}
          onCompleted={() => discoveryFlow.setVerifyDevice(null)}
          title={getVerificationAction(discoveryFlow.verifyDevice.readiness_state).title}
        />
      ) : null}
    </div>
  );
}
