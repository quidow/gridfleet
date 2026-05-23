import { Suspense, lazy } from 'react';
import { useParams } from 'react-router-dom';
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
import { HostDiagnosticsPanel } from '../components/hostDetail/HostDiagnosticsPanel';

const HostResourceTelemetryPanel = lazy(() =>
  import('../components/hostDetail/HostResourceTelemetryPanel').then((m) => ({ default: m.HostResourceTelemetryPanel })),
);
import { HostDevicesPanel } from '../components/hostDetail/HostDevicesPanel';
import { HostDriversPanel } from '../components/hostDetail/HostDriversPanel';
import { HostPluginsPanel } from '../components/hostDetail/HostPluginsPanel';
import { HostLogsPanel } from '../components/hostDetail/HostLogsPanel';
import type { HostDetail as HostDetailType } from '../types';
// HostDetail type alias avoids shadowing the default-exported component name

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'diagnostics', label: 'Diagnostics' },
  { id: 'logs', label: 'Logs' },
  { id: 'devices', label: 'Devices' },
  { id: 'drivers', label: 'Drivers' },
  { id: 'plugins', label: 'Plugins' },
] as const;

const TAB_IDS = TABS.map((t) => t.id);

export function HostDetail() {
  const { id } = useParams<{ id: string }>();
  const { data: host, isLoading, dataUpdatedAt } = useHost(id!);
  usePageTitle(host?.hostname ?? 'Host');
  const approveMut = useApproveHost();
  const rejectMut = useRejectHost();
  const discoveryFlow = useHostDiscoveryFlow(id ?? null);
  const [tab, setTab] = useTabParam('tab', TAB_IDS as unknown as string[], 'overview');

  if (isLoading) {
    return <LoadingSpinner />;
  }

  if (!host) return null;

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
          <SectionErrorBoundary scope="host-diagnostics">
            <HostDiagnosticsPanel host={host} />
          </SectionErrorBoundary>
          <SectionErrorBoundary scope="host-resource-telemetry">
            <Suspense fallback={<div className="h-48 animate-pulse rounded-md border border-border bg-surface-1" />}>
              <HostResourceTelemetryPanel hostId={id!} hostOnline={hostOnline} />
            </Suspense>
          </SectionErrorBoundary>
        </div>
      )}

      {tab === 'logs' && <HostLogsPanel hostId={id!} />}

      {tab === 'devices' && (
        <SectionErrorBoundary scope="host-devices">
          <HostDevicesPanel host={hostDetail} />
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
