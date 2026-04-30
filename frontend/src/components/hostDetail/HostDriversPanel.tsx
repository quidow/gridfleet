import { AlertTriangle, CheckCircle2, MinusCircle } from 'lucide-react';
import { useDriverPackCatalog, useHostDriverPacks } from '../../hooks/useDriverPacks';
import { DataTable } from '../ui';
import type { DataTableColumn } from '../ui';
import type { HostPackFeatureStatus, HostPackStatus } from '../../types/driverPacks';
import HostFeatureActionButton from './HostFeatureActionButton';

function PackStatusBadge({ status, blockedReason }: { status: string; blockedReason: string | null }) {
  const healthy = status === 'installed';
  const blocked = status === 'blocked';
  return (
    <span
      className={`inline-flex w-fit items-center gap-1 rounded px-2 py-0.5 text-xs font-medium ${
        healthy
          ? 'bg-success-soft text-success-foreground'
          : blocked
            ? 'bg-danger-soft text-danger-foreground'
            : 'bg-neutral-soft text-neutral-foreground'
      }`}
      title={blockedReason ?? status}
    >
      {healthy ? <CheckCircle2 size={12} /> : blocked ? <AlertTriangle size={12} /> : <MinusCircle size={12} />}
      {blockedReason ?? status}
    </span>
  );
}

function FeatureStatusBadge({ status }: { status: HostPackFeatureStatus | undefined }) {
  if (!status) return null;
  const healthy = status.ok;
  return (
    <span
      className={`inline-flex w-fit items-center gap-1 rounded px-2 py-0.5 text-xs font-medium ${
        healthy ? 'bg-success-soft text-success-foreground' : 'bg-danger-soft text-danger-foreground'
      }`}
      title={status.detail}
    >
      {healthy ? <CheckCircle2 size={12} /> : <AlertTriangle size={12} />}
      {status.detail || (healthy ? 'healthy' : 'degraded')}
    </span>
  );
}

type Props = {
  hostId: string;
  hostOnline: boolean;
};

export default function HostDriversPanel({ hostId }: Props) {
  const { data: hostPacks, isLoading } = useHostDriverPacks(hostId);
  const { data: catalog } = useDriverPackCatalog();

  const rows = hostPacks?.packs ?? [];
  const runtimeById = new Map((hostPacks?.runtimes ?? []).map((runtime) => [runtime.runtime_id, runtime]));
  const catalogById = new Map((catalog ?? []).map((pack) => [pack.id, pack]));
  const featureStatusByPack = new Map<string, Map<string, HostPackFeatureStatus>>();
  for (const status of hostPacks?.features ?? []) {
    const byFeature = featureStatusByPack.get(status.pack_id) ?? new Map<string, HostPackFeatureStatus>();
    byFeature.set(status.feature_id, status);
    featureStatusByPack.set(status.pack_id, byFeature);
  }

  const packColumns: DataTableColumn<HostPackStatus>[] = [
    {
      key: 'pack_id',
      header: 'Driver',
      render: (p) => {
        const catalogPack = catalogById.get(p.pack_id);
        const features = catalogPack?.features ?? {};
        const featureEntries = Object.entries(features);
        return (
          <div className="flex flex-col gap-1.5">
            <span className="text-sm font-medium text-text-1">{p.pack_id}</span>
            {featureEntries.length > 0 && p.status === 'installed' ? (
              <div className="flex flex-col gap-1">
                {featureEntries.map(([featureId, feature]) => {
                  const status = featureStatusByPack.get(p.pack_id)?.get(featureId);
                  return (
                    <div key={featureId} className="flex flex-wrap items-center gap-1">
                      <span className="text-xs font-medium text-text-2">{feature.display_name}</span>
                      <FeatureStatusBadge status={status} />
                      {feature.actions.map((action) => (
                        <HostFeatureActionButton
                          key={`${featureId}-${action.id}`}
                          hostId={hostId}
                          packId={p.pack_id}
                          featureId={featureId}
                          action={action}
                        />
                      ))}
                    </div>
                  );
                })}
              </div>
            ) : null}
          </div>
        );
      },
    },
    {
      key: 'pack_release',
      header: 'Release',
      render: (p) => <span className="font-mono text-sm text-text-2">{p.pack_release}</span>,
    },
    {
      key: 'appium_driver',
      header: 'Appium Driver',
      render: (p) => {
        const installed = p.installed_appium_driver_version;
        const desired = p.desired_appium_driver_version;
        if (!installed && !desired) {
          return <span className="text-sm text-text-3">-</span>;
        }
        return (
          <div className="flex flex-col gap-0.5">
            <span className="font-mono text-sm text-text-2">{installed ?? 'not installed'}</span>
            {p.appium_driver_drift && desired && (
              <span className="text-xs text-warning-foreground">wanted: {desired}</span>
            )}
          </div>
        );
      },
    },
    {
      key: 'runtime_id',
      header: 'Runtime',
      render: (p) => {
        const runtime = p.runtime_id ? runtimeById.get(p.runtime_id) : undefined;
        if (!runtime) {
          return <span className="font-mono text-sm text-text-3">{p.runtime_id ?? '-'}</span>;
        }
        return (
          <span className="font-mono text-sm text-text-2" title={`appium@${runtime.appium_server_version}`}>
            {runtime.runtime_id}
          </span>
        );
      },
    },
    {
      key: 'status',
      header: 'Status',
      render: (p) => <PackStatusBadge status={p.status} blockedReason={p.blocked_reason} />,
    },
  ];

  return (
    <div className="rounded-lg border border-border bg-surface-1">
      <div className="flex items-center justify-between border-b border-border px-5 py-4">
        <h2 className="text-sm font-medium text-text-2">Appium Drivers</h2>
      </div>
      <DataTable<HostPackStatus>
        columns={packColumns}
        rows={rows}
        rowKey={(p) => p.pack_id}
        loading={isLoading}
        emptyState={
          <p className="px-5 py-8 text-center text-sm text-text-3">
            No drivers installed. Enable drivers in Settings.
          </p>
        }
      />
    </div>
  );
}
