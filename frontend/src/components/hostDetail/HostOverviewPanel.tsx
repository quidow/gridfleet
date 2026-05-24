import { Suspense, lazy } from 'react';
import { formatHostTimestamp } from '../hosts/hostFormatting';
import { HostActionButtons, HostAgentVersionNotice } from '../hosts/hostPresentation';
import type { HostRead } from '../../types';
import { useHostDiagnostics } from '../../hooks/useHosts';
import { DefinitionList } from '../ui/DefinitionList';
import { Card } from '../ui/Card';
import { HostOverviewResourceStrip } from './HostOverviewResourceStrip';
import { HostToolVersionsPanel } from './HostToolVersionsPanel';
import { HostCircuitBreakerCard } from './HostCircuitBreakerCard';
import { EMPTY_GLYPH } from '../../utils/emptyValue';

const HostResourceTelemetryPanel = lazy(() =>
  import('./HostResourceTelemetryPanel').then((m) => ({ default: m.HostResourceTelemetryPanel })),
);

type Props = {
  host: HostRead;
  approvePending: boolean;
  rejectPending: boolean;
  onApprove: () => void;
  onReject: () => void;
};

export function HostOverviewPanel({
  host,
  approvePending,
  rejectPending,
  onApprove,
  onReject,
}: Props) {
  const hostOnline = host.status === 'online';
  const { data: diagnostics } = useHostDiagnostics(host.id);
  const isPending = host.status === 'pending';

  return (
    <div className="space-y-6">
      {isPending && (
        <Card padding="none" className="p-5">
          <div className="mb-3 flex items-baseline justify-between gap-3">
            <h2 className="text-sm font-medium text-text-3">Actions</h2>
            <span className="text-xs text-text-3">Host-scoped</span>
          </div>
          <div className="flex flex-wrap gap-2">
            <HostActionButtons
              status={host.status}
              variant="detail"
              onApprove={onApprove}
              onReject={onReject}
              onDiscover={() => {}}
              approvePending={approvePending}
              rejectPending={rejectPending}
            />
          </div>
        </Card>
      )}

      {/* Tier 1: Identity & Health */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card padding="none">
          <div className="border-b border-border px-5 py-4">
            <h2 className="text-sm font-medium text-text-2">Host Info</h2>
          </div>
          <div className="p-5">
          <DefinitionList
            layout="justified"
            items={[
              { term: 'IP Address', definition: host.ip },
              { term: 'OS', definition: host.os_version ?? host.os_type },
              { term: 'Kernel', definition: host.kernel_version ?? EMPTY_GLYPH },
              { term: 'Architecture', definition: host.cpu_arch ?? EMPTY_GLYPH },
              { term: 'CPU', definition: host.cpu_model ?? EMPTY_GLYPH },
              { term: 'Cores', definition: host.cpu_cores != null ? String(host.cpu_cores) : EMPTY_GLYPH },
              { term: 'Agent Port', definition: String(host.agent_port) },
              { term: 'Status', definition: host.status },
              { term: 'Agent Version', definition: host.agent_version ?? EMPTY_GLYPH },
              { term: 'Last Heartbeat', definition: formatHostTimestamp(host.last_heartbeat) },
              { term: 'Created', definition: formatHostTimestamp(host.created_at) },
            ]}
          />
          <HostAgentVersionNotice
            version={host.agent_version}
            status={host.agent_version_status}
            requiredVersion={host.required_agent_version}
            recommendedVersion={host.recommended_agent_version}
            updateAvailable={host.agent_update_available}
          />
          </div>
        </Card>

        <div className="flex flex-col gap-6">
          <HostOverviewResourceStrip
            hostId={host.id}
            totalCpuCores={host.cpu_cores ?? null}
            totalMemoryMb={host.total_memory_mb ?? null}
            totalDiskGb={host.total_disk_gb ?? null}
          />
          {diagnostics ? (
            <HostCircuitBreakerCard breaker={diagnostics.circuit_breaker} />
          ) : (
            <Card padding="none">
              <div className="border-b border-border px-5 py-4">
                <h2 className="text-sm font-medium text-text-2">Circuit Breaker</h2>
              </div>
              <p className="p-5 text-sm text-text-3">Loading...</p>
            </Card>
          )}
        </div>
      </div>

      {/* Tier 2: Tool Versions */}
      <HostToolVersionsPanel host={host} />

      {/* Tier 3: Resource Monitoring */}
      <Suspense fallback={<div className="h-48 animate-pulse rounded-md border border-border bg-surface-1" />}>
        <HostResourceTelemetryPanel hostId={host.id} hostOnline={hostOnline} />
      </Suspense>

    </div>
  );
}
