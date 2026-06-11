import { useState } from 'react';
import { useApproveHost, useCreateHost, useDeleteHost, useRejectHost } from '../hooks/useHosts';
import { ConfirmDialog } from '../components/ui/ConfirmDialog';
import { SetupVerificationModal } from './devices/SetupVerificationModal';
import { AddHostModal } from '../components/hosts/AddHostModal';
import { HostDiscoveryModal } from '../components/hosts/HostDiscoveryModal';
import { useHostDiscoveryFlow } from '../components/hosts/useHostDiscoveryFlow';
import { getVerificationAction } from '../lib/deviceWorkflow';
import { usePageTitle } from '../hooks/usePageTitle';
import { SectionErrorBoundary } from '../components/ErrorBoundary';
import { HostsTableSection } from './hosts/HostsTableSection';

export function Hosts() {
  usePageTitle('Hosts');
  const createHost = useCreateHost();
  const deleteHostMut = useDeleteHost();
  const approveMut = useApproveHost();
  const rejectMut = useRejectHost();
  const [showAdd, setShowAdd] = useState(false);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [discoveryHostId, setDiscoveryHostId] = useState<string | null>(null);
  const discoveryFlow = useHostDiscoveryFlow(discoveryHostId);

  async function handleDiscover(hostId: string) {
    setDiscoveryHostId(hostId);
    await discoveryFlow.handleDiscover(hostId);
  }

  return (
    <div className="flex h-full flex-col">
      <SectionErrorBoundary scope="hosts-table">
        <HostsTableSection
          onDeleteRequest={setDeleteId}
          onApprove={(id) => approveMut.mutate(id)}
          onReject={(id) => rejectMut.mutate(id)}
          onDiscover={handleDiscover}
          onAddHost={() => setShowAdd(true)}
          approvePending={approveMut.isPending}
          rejectPending={rejectMut.isPending}
          discoverPending={discoveryFlow.discoverMut.isPending}
        />
      </SectionErrorBoundary>

      <AddHostModal
        isOpen={showAdd}
        isPending={createHost.isPending}
        onClose={() => setShowAdd(false)}
        onSubmit={async (form) => {
          await createHost.mutateAsync(form);
          setShowAdd(false);
        }}
      />

      <ConfirmDialog
        isOpen={!!deleteId}
        onClose={() => setDeleteId(null)}
        onConfirm={() => {
          if (deleteId) {
            deleteHostMut.mutate(deleteId);
          }
        }}
        title="Delete Host"
        message="Are you sure you want to delete this host? This will not remove its devices."
        confirmLabel="Delete"
        variant="danger"
      />

      <HostDiscoveryModal
        discoveryResult={discoveryFlow.discoveryResult}
        isPending={discoveryFlow.confirmMut.isPending}
        onClose={() => {
          setDiscoveryHostId(null);
          discoveryFlow.closeDiscovery();
        }}
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
