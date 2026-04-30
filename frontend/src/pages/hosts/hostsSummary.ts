import type { AgentVersionStatus, DeviceRead, HostRead, HostStatus } from '../../types';

const SUMMARY_PARAM_KEYS = ['status', 'agent_version_status'] as const;
const HOST_STATUSES: readonly HostStatus[] = ['online', 'offline', 'pending'];
const AGENT_VERSION_STATUSES: readonly AgentVersionStatus[] = ['disabled', 'ok', 'outdated', 'unknown'];

interface HostsSummaryFilters {
  status: HostStatus | '';
  agentVersionStatus: AgentVersionStatus | '';
}

export interface HostsFleetStats {
  total: number;
  online: number;
  offline: number;
  staleAgents: number;
  unknownAgents: number;
  totalMappedDevices: number;
  offlineMappedDevices: number;
}

interface HostsSummaryHrefOptions {
  status?: HostStatus | null;
  agentVersionStatus?: AgentVersionStatus | null;
}

function readEnumSearchParam<T extends string>(
  searchParams: URLSearchParams,
  key: string,
  allowedValues: readonly T[],
): T | '' {
  const value = searchParams.get(key);
  return value && allowedValues.includes(value as T) ? (value as T) : '';
}

export function readHostsSummaryFilters(searchParams: URLSearchParams): HostsSummaryFilters {
  return {
    status: readEnumSearchParam(searchParams, 'status', HOST_STATUSES),
    agentVersionStatus: readEnumSearchParam(searchParams, 'agent_version_status', AGENT_VERSION_STATUSES),
  };
}

export function hasActiveHostsSummaryFilters(filters: HostsSummaryFilters): boolean {
  return Boolean(filters.status || filters.agentVersionStatus);
}

export function deriveHostsFleetStats(hosts: HostRead[], devices: DeviceRead[]): HostsFleetStats {
  const offlineHostIds = new Set(
    hosts.filter((host) => host.status === 'offline').map((host) => host.id),
  );

  return {
    total: hosts.length,
    online: hosts.filter((host) => host.status === 'online').length,
    offline: hosts.filter((host) => host.status === 'offline').length,
    staleAgents: hosts.filter((host) => host.agent_version_status === 'outdated').length,
    unknownAgents: hosts.filter((host) => host.agent_version_status === 'unknown').length,
    totalMappedDevices: devices.length,
    offlineMappedDevices: devices.filter((device) => offlineHostIds.has(device.host_id)).length,
  };
}

export function filterHostsBySummary(
  hosts: HostRead[],
  filters: HostsSummaryFilters,
): HostRead[] {
  return hosts.filter((host) => {
    if (filters.status && host.status !== filters.status) {
      return false;
    }

    if (filters.agentVersionStatus && host.agent_version_status !== filters.agentVersionStatus) {
      return false;
    }

    return true;
  });
}

export function buildHostsSummaryHref(
  searchParams: URLSearchParams,
  options: HostsSummaryHrefOptions = {},
): string {
  const nextParams = new URLSearchParams(searchParams);

  for (const key of SUMMARY_PARAM_KEYS) {
    nextParams.delete(key);
  }

  if (options.status) {
    nextParams.set('status', options.status);
  }
  if (options.agentVersionStatus) {
    nextParams.set('agent_version_status', options.agentVersionStatus);
  }

  const query = nextParams.toString();
  return query ? `/hosts?${query}` : '/hosts';
}
