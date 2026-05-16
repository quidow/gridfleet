import { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { exportPack } from '../api/driverPackAuthoring';
import { LoadingSpinner } from '../components/LoadingSpinner';
import Card from '../components/ui/Card';
import ConfirmDialog from '../components/ui/ConfirmDialog';
import FetchError from '../components/ui/FetchError';
import { Badge, Button, PageHeader, Tabs, useTabParam } from '../components/ui';
import {
  useDeleteDriverPack,
  useDriverDetail,
  useDriverReleases,
  useSetDriverPackCurrentRelease,
} from '../hooks/useDriverDetail';
import { useSetDriverPackState } from '../hooks/useDriverPacks';
import { usePageTitle } from '../hooks/usePageTitle';
import type { DriverPack } from '../types/driverPacks';
import DriverDetailStatusPills from './driverDetail/DriverDetailStatusPills';
import DriverHostsPanel from './driverDetail/DriverHostsPanel';
import DriverOperationsPanel from './driverDetail/DriverOperationsPanel';
import DriverOverviewPanel from './driverDetail/DriverOverviewPanel';
import DriverPlatformCards from './driverDetail/DriverPlatformCards';
import DriverRuntimePanel from './driverDetail/DriverRuntimePanel';
import { hasPackOperations } from './driverDetail/driverDetailFormat';

const BASE_TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'platforms', label: 'Platforms' },
  { id: 'runtime', label: 'Runtime' },
  { id: 'releases', label: 'Releases' },
  { id: 'hosts', label: 'Hosts' },
  { id: 'manifest', label: 'Manifest' },
] as const;

function ManifestPanel({ pack }: { pack: DriverPack }) {
  const operationCount = Object.keys(pack.features ?? {}).length;

  return (
    <Card padding="md">
      <h2 className="mb-3 text-sm font-semibold text-text-1">Manifest Snapshot</h2>
      <dl className="grid gap-x-4 gap-y-2 text-sm sm:grid-cols-[max-content_1fr]">
        <dt className="text-text-3">Pack ID</dt>
        <dd className="break-all font-mono text-text-1">{pack.id}</dd>
        <dt className="text-text-3">Release</dt>
        <dd className="text-text-1">{pack.current_release ?? 'No release'}</dd>
        <dt className="text-text-3">Maintainer</dt>
        <dd className="text-text-1">{pack.maintainer || 'None'}</dd>
        <dt className="text-text-3">License</dt>
        <dd className="text-text-1">{pack.license || 'None'}</dd>
        <dt className="text-text-3">Platforms</dt>
        <dd className="text-text-1">{pack.platforms?.length ?? 0}</dd>
        <dt className="text-text-3">Operations</dt>
        <dd className="text-text-1">{operationCount}</dd>
        <dt className="text-text-3">Workarounds</dt>
        <dd className="text-text-1">{pack.workarounds?.length ?? 0}</dd>
        <dt className="text-text-3">Doctor Checks</dt>
        <dd className="text-text-1">{pack.doctor?.length ?? 0}</dd>
        <dt className="text-text-3">Insecure Features</dt>
        <dd className="text-text-1">{pack.insecure_features?.length ?? 0}</dd>
        {pack.derived_from && (
          <>
            <dt className="text-text-3">Derived From</dt>
            <dd className="text-text-1">
              {pack.derived_from.pack_id} @ {pack.derived_from.release}
            </dd>
          </>
        )}
      </dl>
    </Card>
  );
}

function ReleasesPanel({ packId }: { packId: string }) {
  const { data, isLoading, error, refetch } = useDriverReleases(packId);
  const setCurrentRelease = useSetDriverPackCurrentRelease();

  if (isLoading) return <LoadingSpinner />;
  if (error) return <FetchError message="Failed to load driver pack releases." onRetry={() => void refetch()} />;

  return (
    <Card padding="md">
      <h2 className="mb-3 text-sm font-semibold text-text-1">Uploaded Releases</h2>
      <div className="divide-y divide-border">
        {(data?.releases ?? []).map((release) => (
          <div key={release.release} className="flex flex-wrap items-center justify-between gap-3 py-3">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-sm text-text-1">{release.release}</span>
                {release.is_current && <Badge tone="success">current</Badge>}
              </div>
              <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-text-3">
                <span>{release.platform_ids.length} platform{release.platform_ids.length === 1 ? '' : 's'}</span>
                {release.artifact_sha256 && <span className="font-mono">{release.artifact_sha256.slice(0, 12)}</span>}
              </div>
            </div>
            <Button
              size="sm"
              variant={release.is_current ? 'secondary' : 'primary'}
              disabled={release.is_current || setCurrentRelease.isPending}
              onClick={() => setCurrentRelease.mutate({ packId, release: release.release })}
            >
              {release.is_current ? 'Current' : `Switch to ${release.release}`}
            </Button>
          </div>
        ))}
      </div>
    </Card>
  );
}

export default function DriverDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const packId = decodeURIComponent(id ?? '');
  const { data: pack, isLoading, error, refetch } = useDriverDetail(packId);
  const toggleMutation = useSetDriverPackState();
  const deleteMutation = useDeleteDriverPack();
  const [exporting, setExporting] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const tabs = pack && hasPackOperations(pack) ? [...BASE_TABS, { id: 'operations', label: 'Operations' }] : BASE_TABS;
  const tabIds = tabs.map((tabItem) => tabItem.id);
  const [tab, setTab] = useTabParam('tab', tabIds, 'overview');
  usePageTitle(pack?.display_name ?? 'Driver Pack');

  if (isLoading) return <LoadingSpinner />;
  if (error || !pack) {
    return (
      <div className="py-6">
        <FetchError
          message="Driver pack not found or could not be loaded."
          onRetry={() => void refetch()}
        />
      </div>
    );
  }
  async function handleExport() {
    if (!pack?.current_release) return;
    setExporting(true);
    try {
      await exportPack(pack.id, pack.current_release);
    } finally {
      setExporting(false);
    }
  }

  const actions = (
    <div className="flex flex-wrap gap-2">
      {pack.state === 'enabled' && (
        <Button
          variant="secondary"
          size="sm"
          disabled={toggleMutation.isPending}
          onClick={() => toggleMutation.mutate({ packId: pack.id, state: 'disabled' })}
        >
          Disable
        </Button>
      )}
      {(pack.state === 'disabled' || pack.state === 'draining') && (
        <Button
          size="sm"
          disabled={toggleMutation.isPending}
          onClick={() => toggleMutation.mutate({ packId: pack.id, state: 'enabled' })}
        >
          Enable
        </Button>
      )}
      <Button variant="secondary" size="sm" disabled={exporting || !pack.current_release} onClick={() => void handleExport()}>
        {exporting ? 'Exporting...' : 'Export Tarball'}
      </Button>
      <Button
        variant="danger"
        size="sm"
        disabled={deleteMutation.isPending}
        onClick={() => setDeleteOpen(true)}
      >
        Delete
      </Button>
    </div>
  );

  return (
    <div>
      <PageHeader
        title={pack.display_name}
        subtitle={`${pack.id} - ${pack.current_release ?? 'no release'}`}
        actions={actions}
        summary={<DriverDetailStatusPills pack={pack} />}
      />

      <Tabs tabs={tabs as unknown as { id: string; label: string }[]} activeId={tab} onChange={setTab} className="mb-6" />

      <div className="fade-in-stagger flex flex-col gap-6">
        {tab === 'overview' && <DriverOverviewPanel pack={pack} />}
        {tab === 'platforms' && <DriverPlatformCards platforms={pack.platforms ?? []} />}
        {tab === 'runtime' && <DriverRuntimePanel pack={pack} />}
        {tab === 'releases' && <ReleasesPanel packId={pack.id} />}
        {tab === 'hosts' && <DriverHostsPanel packId={pack.id} />}
        {tab === 'manifest' && <ManifestPanel pack={pack} />}
        {tab === 'operations' && <DriverOperationsPanel pack={pack} />}
      </div>

      <ConfirmDialog
        isOpen={deleteOpen}
        onClose={() => setDeleteOpen(false)}
        onConfirm={() =>
          deleteMutation.mutate(pack.id, {
            onSuccess: () => navigate('/drivers'),
          })
        }
        title="Delete Driver Pack"
        message={`Delete ${pack.display_name}? This removes the pack from desired state and deletes stored tarball artifacts. Devices or active work that still reference the pack will block deletion.`}
        confirmLabel="Delete"
        variant="danger"
      />
    </div>
  );
}
