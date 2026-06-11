import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  approveHost,
  confirmDiscovery,
  createHost,
  deleteHost,
  discoverDevices,
  fetchHostAgentLogs,
  fetchHostEvents,
  fetchIntakeCandidates,
  fetchHost,
  fetchHostResourceTelemetry,
  fetchHostDiagnostics,
  fetchHostToolStatus,
  fetchHosts,
  rejectHost,
} from '../api/hosts';
import type { AgentLogQuery, HostEventsQuery } from '../api/hosts';
import type { DiscoveryConfirm, HostCreate } from '../types';
import { useEventStreamStatus } from '../context/EventStreamContext';
import { sseAdaptivePolling, POLL_FAST_MS, POLL_DEFAULT_MS, POLL_RELAXED_MS, POLL_SLOW_MS } from './polling';
import { qk } from '../lib/queryKeys';

export function useHosts() {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.hosts.root,
    queryFn: fetchHosts,
    ...sseAdaptivePolling(connected, POLL_RELAXED_MS),
  });
}

export function useHost(id: string) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.host.detail(id),
    queryFn: () => fetchHost(id),
    ...sseAdaptivePolling(connected, POLL_DEFAULT_MS),
  });
}

export function useHostDiagnostics(id: string) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.hostDiagnostics.byHost(id),
    queryFn: () => fetchHostDiagnostics(id),
    ...sseAdaptivePolling(connected, POLL_DEFAULT_MS),
    enabled: !!id,
  });
}

export function useHostResourceTelemetry(id: string) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.hostResourceTelemetry.byHost(id),
    queryFn: () => fetchHostResourceTelemetry(id),
    ...sseAdaptivePolling(connected, POLL_SLOW_MS),
    enabled: !!id,
  });
}

export function useHostToolStatus(id: string, enabled = true) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.hostToolsStatus.byHost(id),
    queryFn: () => fetchHostToolStatus(id),
    ...sseAdaptivePolling(connected, POLL_RELAXED_MS),
    enabled: !!id && enabled,
  });
}

export function useHostAgentLogs(hostId: string, filters: AgentLogQuery) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.hostAgentLogs.list(hostId, filters),
    queryFn: () => fetchHostAgentLogs(hostId, filters),
    ...sseAdaptivePolling(connected, POLL_FAST_MS, POLL_SLOW_MS),
    refetchIntervalInBackground: false,
    enabled: Boolean(hostId),
  });
}

export function useHostEvents(hostId: string, filters: HostEventsQuery) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.hostEvents.list(hostId, filters),
    queryFn: () => fetchHostEvents(hostId, filters),
    ...sseAdaptivePolling(connected, POLL_FAST_MS, POLL_SLOW_MS),
    refetchIntervalInBackground: false,
    enabled: Boolean(hostId),
  });
}

export function useCreateHost() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: HostCreate) => createHost(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.hosts.root }),
  });
}

export function useDeleteHost() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteHost(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.hosts.root }),
  });
}

export function useDiscoverDevices() {
  return useMutation({
    mutationFn: (hostId: string) => discoverDevices(hostId),
  });
}

export function useIntakeCandidates(hostId: string | null) {
  return useQuery({
    queryKey: qk.intakeCandidates.byHost(hostId),
    queryFn: () => fetchIntakeCandidates(hostId!),
    enabled: !!hostId,
    refetchInterval: POLL_FAST_MS,
    staleTime: 2_500,
  });
}

export function useConfirmDiscovery() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ hostId, body }: { hostId: string; body: DiscoveryConfirm }) =>
      confirmDiscovery(hostId, body),
    onSuccess: (_data, { hostId }) => {
      qc.invalidateQueries({ queryKey: qk.hosts.root });
      qc.invalidateQueries({ queryKey: qk.host.detail(hostId) });
      qc.invalidateQueries({ queryKey: qk.devices.root });
    },
  });
}

export function useApproveHost() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => approveHost(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.hosts.root });
      qc.invalidateQueries({ queryKey: qk.host.root });
    },
  });
}

export function useRejectHost() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => rejectHost(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.hosts.root });
    },
  });
}

