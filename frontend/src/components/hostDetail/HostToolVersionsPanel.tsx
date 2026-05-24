import { AlertTriangle } from 'lucide-react';
import { useHostToolStatus } from '../../hooks/useHosts';
import { Card } from '../ui/Card';
import type { HostRead } from '../../types';

type ToolEntry = {
  name: string;
  version: string | null;
  description: string;
};

type Props = {
  host: HostRead;
};

function ToolCell({ tool }: { tool: ToolEntry }) {
  const missing = !tool.version;
  return (
    <div className="px-5 py-4">
      <div className="text-xs font-medium uppercase text-text-3">{tool.name}</div>
      <div className={`mt-1 font-mono text-sm ${missing ? 'flex items-center gap-1 text-warning-foreground' : 'text-text-1'}`}>
        {missing ? (
          <>
            <AlertTriangle size={14} />
            <span>not found</span>
          </>
        ) : (
          tool.version
        )}
      </div>
      <div className="mt-1 text-xs text-text-3">{tool.description}</div>
    </div>
  );
}

export function HostToolVersionsPanel({ host }: Props) {
  const hostId = host.id;
  const hostOnline = host.status === 'online';
  const { data: toolStatus, isLoading: toolsLoading } = useHostToolStatus(hostId, hostOnline);

  const offlineMessage = (
    <p className="px-5 py-8 text-center text-sm text-text-3">Host must be online to read tool versions.</p>
  );

  const hostTools = toolStatus?.host ? Object.values(toolStatus.host) : [];
  const packEntries = toolStatus?.packs ? Object.entries(toolStatus.packs) : [];
  const hasPackDeps = packEntries.some(([, tools]) => tools.length > 0);

  return (
    <div className="space-y-6">
      <Card padding="none">
        <div className="border-b border-border px-5 py-4">
          <h2 className="text-sm font-medium text-text-2">Host Tools</h2>
        </div>
        {!hostOnline ? offlineMessage : toolsLoading ? (
          <p className="px-5 py-8 text-center text-sm text-text-3">Loading tool versions...</p>
        ) : !toolStatus ? (
          <p className="px-5 py-8 text-center text-sm text-text-3">Tool versions are currently unavailable.</p>
        ) : (
          <div className="grid grid-cols-1 divide-y divide-border md:grid-cols-2 md:divide-x md:divide-y-0">
            {hostTools.map((tool) => (
              <ToolCell key={tool.name} tool={tool} />
            ))}
          </div>
        )}
      </Card>

      <Card padding="none">
        <div className="border-b border-border px-5 py-4">
          <h2 className="text-sm font-medium text-text-2">Driver Pack Dependencies</h2>
        </div>
        {!hostOnline ? offlineMessage : toolsLoading ? (
          <p className="px-5 py-8 text-center text-sm text-text-3">Loading tool versions...</p>
        ) : !toolStatus ? (
          <p className="px-5 py-8 text-center text-sm text-text-3">Tool versions are currently unavailable.</p>
        ) : !hasPackDeps ? (
          <p className="px-5 py-8 text-center text-sm text-text-3">No driver packs installed.</p>
        ) : (
          <div className="divide-y divide-border">
            {packEntries.map(([packId, tools]) =>
              tools.length > 0 ? (
                <div key={packId}>
                  <div className="px-5 pt-4 pb-2">
                    <span className="font-mono text-sm font-medium text-text-2">{packId}</span>
                  </div>
                  <div className="grid grid-cols-1 divide-y divide-border/50 md:grid-cols-2 md:divide-x md:divide-y-0">
                    {tools.map((tool) => (
                      <ToolCell key={tool.name} tool={tool} />
                    ))}
                  </div>
                </div>
              ) : null,
            )}
          </div>
        )}
      </Card>
    </div>
  );
}
