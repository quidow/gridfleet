import SummaryPill, { type SummaryPillTone } from '../../components/ui/SummaryPill';
import type { AgentVersionStatus, HostDetail, HostStatus } from '../../types';

type Props = { host: HostDetail };

const STATUS_LABEL: Record<HostStatus, string> = {
  online: 'Online',
  offline: 'Offline',
  pending: 'Pending',
};

const STATUS_TONE: Record<HostStatus, SummaryPillTone> = {
  online: 'ok',
  offline: 'error',
  pending: 'warn',
};

const AGENT_LABEL: Record<AgentVersionStatus, string> = {
  ok: 'Up to date',
  outdated: 'Outdated',
  unknown: 'Unknown',
  disabled: 'Disabled',
};

const AGENT_TONE: Record<AgentVersionStatus, SummaryPillTone> = {
  ok: 'ok',
  outdated: 'warn',
  unknown: 'neutral',
  disabled: 'neutral',
};

export default function HostDetailStatusPills({ host }: Props) {
  const deviceCount = host.devices.length;
  const missingPrereqs = host.missing_prerequisites?.length ?? 0;

  return (
    <>
      <SummaryPill tone={STATUS_TONE[host.status]} label="Status" value={STATUS_LABEL[host.status]} />
      <SummaryPill
        tone={AGENT_TONE[host.agent_version_status]}
        label="Agent"
        value={host.agent_version ? `${host.agent_version} · ${AGENT_LABEL[host.agent_version_status]}` : AGENT_LABEL[host.agent_version_status]}
      />
      <SummaryPill tone="neutral" label="Devices" value={deviceCount} />
      {missingPrereqs > 0 ? (
        <SummaryPill tone="warn" label="Prerequisites missing" value={missingPrereqs} />
      ) : null}
    </>
  );
}
