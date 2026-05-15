import { formatHostTimestamp } from '../hosts/hostFormatting';
import { HostActionButtons, HostAgentVersionNotice } from '../hosts/hostPresentation';
import type { HostRead } from '../../types';
import DefinitionList from '../ui/DefinitionList';
import HostOverviewResourceStrip from './HostOverviewResourceStrip';
import HostToolVersionsPanel from './HostToolVersionsPanel';
import { EMPTY_GLYPH } from '../../utils/emptyValue';

type Props = {
  host: HostRead;
  approvePending: boolean;
  rejectPending: boolean;
  discoverPending: boolean;
  onApprove: () => void;
  onReject: () => void;
  onDiscover: () => void;
};

export default function HostOverviewPanel({
  host,
  approvePending,
  rejectPending,
  discoverPending,
  onApprove,
  onReject,
  onDiscover,
}: Props) {
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <div className="rounded-lg border border-border bg-surface-1 p-5">
          <h2 className="mb-4 text-sm font-medium text-text-3">Host Info</h2>
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

        <div className="flex flex-col gap-6">
          <HostOverviewResourceStrip hostId={host.id} />
          <div className="rounded-lg border border-border bg-surface-1 p-5">
            <div className="mb-3 flex items-baseline justify-between gap-3">
              <h2 className="text-sm font-medium text-text-3">Actions</h2>
              <span className="text-xs text-text-3">Host-scoped</span>
            </div>
            <p className="mb-3 text-xs text-text-3">
              Discovery only checks devices visible to this agent.
            </p>
            <div className="flex flex-wrap gap-2">
              <HostActionButtons
                status={host.status}
                variant="detail"
                onApprove={onApprove}
                onReject={onReject}
                onDiscover={onDiscover}
                approvePending={approvePending}
                rejectPending={rejectPending}
                discoverPending={discoverPending}
              />
            </div>
          </div>
        </div>
      </div>

      <HostToolVersionsPanel host={host} />
    </div>
  );
}
