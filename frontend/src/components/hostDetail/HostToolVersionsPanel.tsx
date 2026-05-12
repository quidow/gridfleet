import { AlertTriangle } from 'lucide-react';
import { useHostToolStatus } from '../../hooks/useHosts';
import { describeHostPrerequisite } from '../../lib/hostPrerequisites';
import type { HostRead } from '../../types';

function formatToolValue(value: string | null | undefined) {
  return value && value.trim() ? value : '-';
}

type Props = {
  host: HostRead;
};

export default function HostToolVersionsPanel({ host }: Props) {
  const hostId = host.id;
  const hostOnline = host.status === 'online';
  const missingPrerequisites = host.missing_prerequisites ?? [];
  const { data: toolStatus, isLoading: toolsLoading, error: toolsError } = useHostToolStatus(hostId, hostOnline);

  return (
    <div className="space-y-6">
      <div className="rounded-lg border border-border bg-surface-1">
        <div className="flex items-center justify-between border-b border-border px-5 py-4">
          <h2 className="text-sm font-medium text-text-2">Tool Versions</h2>
        </div>
        {!hostOnline ? (
          <p className="px-5 py-8 text-center text-sm text-text-3">Host must be online to read tool versions.</p>
        ) : toolsLoading ? (
          <p className="px-5 py-8 text-center text-sm text-text-3">Loading tool versions...</p>
        ) : toolsError || !toolStatus ? (
          <p className="px-5 py-8 text-center text-sm text-text-3">Tool versions are currently unavailable.</p>
        ) : (
          <div className="grid grid-cols-1 divide-y divide-border md:grid-cols-3 md:divide-x md:divide-y-0">
            {[
              ['Node', toolStatus.node],
              ['Node Provider', toolStatus.node_provider ?? toolStatus.node_error],
              ['go-ios', toolStatus.go_ios],
            ].map(([label, value]) => (
              <div key={label} className="px-5 py-4">
                <div className="text-xs font-medium uppercase text-text-3">{label}</div>
                <div className="mt-1 font-mono text-sm text-text-1">{formatToolValue(value)}</div>
              </div>
            ))}
          </div>
        )}
      </div>

      {missingPrerequisites.length > 0 ? (
        <div className="rounded-lg border border-warning-strong/30 bg-warning-soft">
          <div className="flex items-center gap-2 border-b border-warning-strong/30 px-5 py-4">
            <AlertTriangle size={16} className="text-warning-foreground" />
            <h2 className="text-sm font-medium text-warning-foreground">Missing Prerequisites</h2>
          </div>
          <div className="divide-y divide-warning-strong/30">
            {missingPrerequisites.map((name) => (
              <div
                key={name}
                className="flex flex-col gap-1 px-5 py-3 text-sm sm:flex-row sm:items-center sm:justify-between"
              >
                <span className="font-mono font-medium text-warning-foreground">{name}</span>
                <span className="text-warning-foreground">{describeHostPrerequisite(name)}</span>
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
