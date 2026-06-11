import { useState } from 'react';
import { AlertTriangle, CheckCircle2, MinusCircle, ChevronDown, ChevronRight, Activity, Loader2 } from 'lucide-react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { useDriverPackCatalog, useHostDriverPacks } from '../../hooks/useDriverPacks';
import { triggerDriverDoctor } from '../../api/driverPacks';
import { DataTable } from '../ui';
import type { DataTableColumn } from '../ui';
import type { HostPackDoctorStatus, HostPackFeatureStatus, HostPackStatus } from '../../types/driverPacks';
import { HostFeatureActionButton } from './HostFeatureActionButton';
import { qk } from '../../lib/queryKeys';

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

export function HostDriversPanel({ hostId }: Props) {
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

  const queryClient = useQueryClient();

  const doctorByPack = new Map<string, HostPackDoctorStatus[]>();
  for (const d of hostPacks?.doctor ?? []) {
    const list = doctorByPack.get(d.pack_id) ?? [];
    list.push(d);
    doctorByPack.set(d.pack_id, list);
  }

  const [expandedPacks, setExpandedPacks] = useState<Set<string>>(new Set());
  const [unsupportedPacks, setUnsupportedPacks] = useState<Set<string>>(new Set());

  const toggleExpanded = (packId: string) => {
    setExpandedPacks((prev) => {
      const next = new Set(prev);
      if (next.has(packId)) next.delete(packId);
      else next.add(packId);
      return next;
    });
  };

  const doctorMutation = useMutation({
    mutationFn: (packId: string) => triggerDriverDoctor(hostId, packId),
    onSuccess: (results, packId) => {
      queryClient.invalidateQueries({ queryKey: qk.hostDriverPacks.byHost(hostId) });
      setUnsupportedPacks((prev) => {
        const next = new Set(prev);
        if (results.length === 0) next.add(packId);
        else next.delete(packId);
        return next;
      });
    },
    onError: (err: Error) => {
      toast.error(`Doctor check failed: ${err.message}`);
    },
  });

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
    {
      key: 'doctor',
      header: 'Doctor',
      render: (p) => {
        if (unsupportedPacks.has(p.pack_id)) {
          return (
            <span className="inline-flex items-center gap-1 rounded px-2 py-0.5 text-xs font-medium bg-neutral-soft text-neutral-foreground">
              <MinusCircle size={12} />
              not supported
            </span>
          );
        }
        const checks = doctorByPack.get(p.pack_id);
        if (!checks || checks.length === 0) {
          return <span className="text-xs text-text-3">No doctor checks</span>;
        }
        const allPassed = checks.every((c) => c.ok);
        const failCount = checks.filter((c) => !c.ok).length;
        const isExpanded = expandedPacks.has(p.pack_id);
        return (
          <button
            type="button"
            className="inline-flex items-center gap-1 text-xs"
            onClick={(e) => {
              e.stopPropagation();
              toggleExpanded(p.pack_id);
            }}
          >
            {isExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            <span
              className={`inline-flex items-center gap-1 rounded px-2 py-0.5 font-medium ${
                allPassed
                  ? 'bg-success-soft text-success-foreground'
                  : 'bg-danger-soft text-danger-foreground'
              }`}
            >
              {allPassed ? (
                <>
                  <CheckCircle2 size={12} />
                  passed ({checks.length})
                </>
              ) : (
                <>
                  <AlertTriangle size={12} />
                  {failCount} failed
                </>
              )}
            </span>
          </button>
        );
      },
    },
    {
      key: 'actions',
      header: '',
      width: '100px',
      render: (p) => {
        if (p.status !== 'installed') return null;
        const isRunning = doctorMutation.isPending && doctorMutation.variables === p.pack_id;
        return (
          <button
            type="button"
            className="inline-flex items-center gap-1 rounded-md border border-border bg-surface-1 px-2.5 py-1.5 text-xs font-medium text-text-2 hover:bg-surface-2 disabled:opacity-50"
            disabled={isRunning}
            onClick={(e) => {
              e.stopPropagation();
              doctorMutation.mutate(p.pack_id);
            }}
          >
            {isRunning ? <Loader2 size={12} className="animate-spin" /> : <Activity size={12} />}
            Run Doctor
          </button>
        );
      },
    },
  ];

  return (
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
      renderExpandedRow={(p) => {
        const checks = doctorByPack.get(p.pack_id);
        if (!expandedPacks.has(p.pack_id) || !checks || checks.length === 0) return null;
        return (
          <div className="flex flex-col gap-1.5">
            {checks.map((c) => (
              <div key={c.check_id} className="flex items-start gap-2 text-xs">
                {c.ok ? (
                  <CheckCircle2 size={12} className="mt-0.5 shrink-0 text-success-foreground" />
                ) : (
                  <AlertTriangle size={12} className="mt-0.5 shrink-0 text-danger-foreground" />
                )}
                <span className="font-medium text-text-2">{c.check_id}</span>
                <span className="text-text-3">{c.message}</span>
              </div>
            ))}
          </div>
        );
      }}
    />
  );
}
