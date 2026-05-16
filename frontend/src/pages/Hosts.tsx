import { useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { AlertTriangle, Plus, Server, Trash2 } from 'lucide-react';
import { useApproveHost, useCreateHost, useDeleteHost, useHosts, useRejectHost } from '../hooks/useHosts';
import { useDevices } from '../hooks/useDevices';
import ConfirmDialog from '../components/ui/ConfirmDialog';
import EmptyState from '../components/ui/EmptyState';
import SetupVerificationModal from './devices/SetupVerificationModal';
import { StatusBadge } from '../components/StatusBadge';
import AddHostModal from '../components/hosts/AddHostModal';
import HostDiscoveryModal from '../components/hosts/HostDiscoveryModal';
import { formatHostLastHeartbeat } from '../components/hosts/hostFormatting';
import {
  HostActionButtons,
  HostAgentVersionIndicator,
} from '../components/hosts/hostPresentation';
import { useHostDiscoveryFlow } from '../components/hosts/useHostDiscoveryFlow';
import { getVerificationAction } from '../lib/deviceWorkflow';
import { formatHostPrerequisiteList } from '../lib/hostPrerequisites';
import { usePageTitle } from '../hooks/usePageTitle';
import DataTable from '../components/ui/DataTable';
import Button from '../components/ui/Button';
import FetchError from '../components/ui/FetchError';
import ListPageSubheader from '../components/ui/ListPageSubheader';
import PageHeader from '../components/ui/PageHeader';
import type { DataTableColumn, DataTableSort } from '../components/ui/DataTable';
import type { HostRead } from '../types';
import HostsSummaryPills from './hosts/HostsSummaryPills';
import {
  deriveHostsFleetStats,
  filterHostsBySummary,
  hasActiveHostsSummaryFilters,
  readHostsSummaryFilters,
} from './hosts/hostsSummary';

type HostSortKey = 'hostname' | 'ip' | 'os_type' | 'status' | 'agent_version' | 'devices' | 'last_heartbeat';

export default function Hosts() {
  usePageTitle('Hosts');
  const [searchParams, setSearchParams] = useSearchParams();
  const { data: hosts = [], isLoading: hostsLoading, isError: hostsError, refetch, dataUpdatedAt: hostsUpdatedAt } = useHosts();
  const { data: devices = [], dataUpdatedAt: devicesUpdatedAt } = useDevices();
  const createHost = useCreateHost();
  const deleteHostMut = useDeleteHost();
  const approveMut = useApproveHost();
  const rejectMut = useRejectHost();
  const [showAdd, setShowAdd] = useState(false);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [discoveryHostId, setDiscoveryHostId] = useState<string | null>(null);
  const [sort, setSort] = useState<DataTableSort<HostSortKey>>({ key: 'hostname', direction: 'asc' });
  const discoveryFlow = useHostDiscoveryFlow(discoveryHostId);
  const filters = useMemo(() => readHostsSummaryFilters(searchParams), [searchParams]);
  const activeSummaryFilters = hasActiveHostsSummaryFilters(filters);
  const showInitialLoading = hostsLoading && hosts.length === 0;

  const deviceCountMap = useMemo(() => {
    const counts = new Map<string, number>();
    for (const device of devices) {
      counts.set(device.host_id, (counts.get(device.host_id) ?? 0) + 1);
    }
    return counts;
  }, [devices]);

  const fleetStats = useMemo(
    () => deriveHostsFleetStats(hosts, devices),
    [devices, hosts],
  );

  const filteredHosts = useMemo(
    () => filterHostsBySummary(hosts, filters),
    [filters, hosts],
  );

  const lastUpdated = useMemo(() => {
    const maxUpdatedAt = Math.max(hostsUpdatedAt ?? 0, devicesUpdatedAt ?? 0);
    return maxUpdatedAt > 0 ? maxUpdatedAt : null;
  }, [devicesUpdatedAt, hostsUpdatedAt]);

  function deviceCountForHost(hostId: string) {
    return deviceCountMap.get(hostId) ?? 0;
  }

  const sortedHosts = [...filteredHosts].sort((leftHost, rightHost) => {
    const direction = sort.direction === 'asc' ? 1 : -1;
    const valueFor = (host: HostRead) => {
      switch (sort.key) {
        case 'hostname': return host.hostname.toLowerCase();
        case 'ip': return host.ip;
        case 'os_type': return host.os_type;
        case 'status': return host.status;
        case 'agent_version': return host.agent_version ?? '';
        case 'devices': return deviceCountForHost(host.id);
        case 'last_heartbeat': return host.last_heartbeat ? new Date(host.last_heartbeat).getTime() : 0;
      }
    };
    const left = valueFor(leftHost);
    const right = valueFor(rightHost);
    if (left < right) return -1 * direction;
    if (left > right) return 1 * direction;
    return leftHost.hostname.localeCompare(rightHost.hostname) * direction;
  });

  function clearSummaryFilters() {
    setSearchParams((current) => {
      const nextParams = new URLSearchParams(current);
      nextParams.delete('status');
      nextParams.delete('agent_version_status');
      return nextParams;
    });
  }

  async function handleDiscover(hostId: string) {
    setDiscoveryHostId(hostId);
    await discoveryFlow.handleDiscover(hostId);
  }

  const columns: DataTableColumn<HostRead, HostSortKey>[] = [
    {
      key: 'hostname',
      header: 'Hostname',
      sortKey: 'hostname',
      render: (host) => (
        <div className="flex items-center gap-2">
          <Link to={`/hosts/${host.id}`} className="font-medium text-accent hover:text-accent-hover text-sm">
            {host.hostname}
          </Link>
          {(host.missing_prerequisites ?? []).length > 0 ? (
            <AlertTriangle
              size={15}
              className="text-warning-strong"
              aria-label={`Missing prerequisites: ${formatHostPrerequisiteList(host.missing_prerequisites ?? [])}`}
            />
          ) : null}
        </div>
      ),
    },
    {
      key: 'ip',
      header: 'IP',
      sortKey: 'ip',
      render: (host) => <span className="text-sm text-text-2">{host.ip}</span>,
    },
    {
      key: 'os_type',
      header: 'OS',
      sortKey: 'os_type',
      render: (host) => <span className="text-sm text-text-2">{host.os_type}</span>,
    },
    {
      key: 'status',
      header: 'Status',
      sortKey: 'status',
      render: (host) => <StatusBadge status={host.status} />,
    },
    {
      key: 'agent_version',
      header: 'Agent Version',
      sortKey: 'agent_version',
      render: (host) => (
        <HostAgentVersionIndicator
          version={host.agent_version}
          status={host.agent_version_status}
          requiredVersion={host.required_agent_version}
          recommendedVersion={host.recommended_agent_version}
          updateAvailable={host.agent_update_available}
        />
      ),
    },
    {
      key: 'devices',
      header: 'Devices',
      sortKey: 'devices',
      render: (host) => <span className="text-sm text-text-2">{deviceCountForHost(host.id)}</span>,
    },
    {
      key: 'last_heartbeat',
      header: 'Last Heartbeat',
      sortKey: 'last_heartbeat',
      render: (host) => <span className="text-sm text-text-3">{formatHostLastHeartbeat(host.last_heartbeat)}</span>,
    },
    {
      key: 'actions',
      header: 'Actions',
      align: 'right',
      render: (host) => (
        <div className="flex items-center justify-end gap-2">
          <HostActionButtons
            status={host.status}
            onApprove={() => approveMut.mutate(host.id)}
            onReject={() => rejectMut.mutate(host.id)}
            onDiscover={() => handleDiscover(host.id)}
            approvePending={approveMut.isPending}
            rejectPending={rejectMut.isPending}
            discoverPending={discoveryFlow.discoverMut.isPending}
          />
          {host.status !== 'pending' ? (
            <button
              onClick={() => setDeleteId(host.id)}
              className="rounded p-1.5 text-text-3 hover:text-danger-foreground"
              title="Delete Host"
            >
              <Trash2 size={16} />
            </button>
          ) : null}
        </div>
      ),
    },
  ];

  const showingFilteredCount = sortedHosts.length !== hosts.length;
  const filteredEmpty = !showInitialLoading && hosts.length > 0 && sortedHosts.length === 0;
  const showingLabel = showingFilteredCount
    ? `Showing ${sortedHosts.length} of ${hosts.length} hosts`
    : `Showing ${sortedHosts.length} hosts`;

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Hosts"
        subtitle="Monitor host reachability, agent drift, and discovery readiness"
        updatedAt={lastUpdated}
        summary={(
          <HostsSummaryPills
            stats={fleetStats}
            searchParams={searchParams}
            isLoading={showInitialLoading}
            disabled={hostsError}
          />
        )}
      />

      {hostsError ? (
        <FetchError
          message="Could not load hosts. Check your connection and try again."
          onRetry={() => void refetch()}
        />
      ) : (
        <div className="fade-in-stagger flex min-h-0 flex-1 flex-col">
          <ListPageSubheader
            title={showingLabel}
            action={
              <div className="flex items-center gap-2">
                {activeSummaryFilters ? (
                  <Button variant="ghost" size="sm" onClick={clearSummaryFilters}>
                    Clear filters
                  </Button>
                ) : null}
                <Button leadingIcon={<Plus size={16} />} onClick={() => setShowAdd(true)}>
                  Add Host
                </Button>
              </div>
            }
          />

          <DataTable<HostRead, HostSortKey>
            columns={columns}
            rows={sortedHosts}
            rowKey={(host) => host.id}
            sort={sort}
            onSortChange={setSort}
            loading={showInitialLoading}
            emptyState={
              filteredEmpty ? (
                <EmptyState
                  icon={Server}
                  title="No hosts match current filters"
                  description="Try clearing filters to widen the fleet view."
                  action={
                    <Button variant="secondary" onClick={clearSummaryFilters}>
                      Clear Filters
                    </Button>
                  }
                />
              ) : (
                <EmptyState
                  icon={Server}
                  title="No hosts registered"
                  description="Add a host to start managing devices."
                  action={
                    <Button leadingIcon={<Plus size={16} />} onClick={() => setShowAdd(true)}>
                      Add Host
                    </Button>
                  }
                />
              )
            }
          />
        </div>
      )}

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
