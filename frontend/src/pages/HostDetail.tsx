import { useParams } from 'react-router-dom';
import { useApproveHost, useHost, useHostCapabilities, useHostDiagnostics, useRejectHost } from '../hooks/useHosts';
import { LoadingSpinner } from '../components/LoadingSpinner';
import SetupVerificationModal from './devices/SetupVerificationModal';
import HostDiscoveryModal from '../components/hosts/HostDiscoveryModal';
import { useHostDiscoveryFlow } from '../components/hosts/useHostDiscoveryFlow';
import { getVerificationAction } from '../lib/deviceWorkflow';
import { usePageTitle } from '../hooks/usePageTitle';
import { PageHeader, Tabs, useTabParam } from '../components/ui';
import FetchError from '../components/ui/FetchError';
import HostDetailStatusPills from './hostDetail/HostDetailStatusPills';
import HostOverviewPanel from '../components/hostDetail/HostOverviewPanel';
import HostDiagnosticsPanel from '../components/hostDetail/HostDiagnosticsPanel';
import HostResourceTelemetryPanel from '../components/hostDetail/HostResourceTelemetryPanel';
import HostDevicesPanel from '../components/hostDetail/HostDevicesPanel';
import HostDriversPanel from '../components/hostDetail/HostDriversPanel';
import HostPluginsPanel from '../components/hostDetail/HostPluginsPanel';
import HostTerminalPanel from '../components/hostDetail/HostTerminalPanel';
import HostLogsPanel from '../components/hostDetail/HostLogsPanel';
import type { HostDetail as HostDetailType } from '../types';
// HostDetail type alias avoids shadowing the default-exported component name

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'diagnostics', label: 'Diagnostics' },
  { id: 'logs', label: 'Logs' },
  { id: 'devices', label: 'Devices' },
  { id: 'drivers', label: 'Drivers' },
  { id: 'plugins', label: 'Plugins' },
  { id: 'terminal', label: 'Terminal' },
] as const;

const TAB_IDS = TABS.map((t) => t.id);

export default function HostDetail() {
  const { id } = useParams<{ id: string }>();
  const { data: host, isLoading, error, dataUpdatedAt } = useHost(id!);
  const { data: hostDiagnostics, isLoading: diagnosticsLoading, error: diagnosticsError } = useHostDiagnostics(id!);
  usePageTitle(host?.hostname ?? 'Host');
  const approveMut = useApproveHost();
  const rejectMut = useRejectHost();
  const discoveryFlow = useHostDiscoveryFlow(id ?? null);
  const [tab, setTab] = useTabParam('tab', TAB_IDS as unknown as string[], 'overview');
  const { data: capabilities } = useHostCapabilities();

  if (isLoading) {
    return <LoadingSpinner />;
  }

  if (error || !host) {
    return (
      <div className="py-6">
        <FetchError
          message="Host not found or could not be loaded."
          onRetry={() => void window.location.reload()}
        />
      </div>
    );
  }

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
        <HostOverviewPanel
          host={host}
          approvePending={approveMut.isPending}
          rejectPending={rejectMut.isPending}
          discoverPending={discoveryFlow.discoverMut.isPending}
          onApprove={() => approveMut.mutate(id!)}
          onReject={() => rejectMut.mutate(id!)}
          onDiscover={() => discoveryFlow.handleDiscover()}
        />
      )}

      {tab === 'diagnostics' && (
        <div className="space-y-6">
          <HostDiagnosticsPanel
            host={host}
            hostDiagnostics={hostDiagnostics}
            diagnosticsLoading={diagnosticsLoading}
            diagnosticsError={diagnosticsError}
          />
          <HostResourceTelemetryPanel hostId={id!} hostOnline={hostOnline} />
        </div>
      )}

      {tab === 'logs' && <HostLogsPanel hostId={id!} />}

      {tab === 'devices' && <HostDevicesPanel host={hostDetail} />}

      {tab === 'drivers' && <HostDriversPanel hostId={id!} hostOnline={hostOnline} />}

      {tab === 'plugins' && <HostPluginsPanel hostId={id!} />}

      {tab === 'terminal' && (
        <HostTerminalPanel
          hostId={id!}
          hostOnline={hostOnline}
          terminalEnabled={capabilities?.web_terminal_enabled ?? false}
        />
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
