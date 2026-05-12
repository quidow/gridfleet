import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  approveHost,
  confirmDiscovery,
  createHost,
  deleteHost,
  discoverDevices,
  fetchIntakeCandidates,
  fetchHost,
  fetchHostResourceTelemetry,
  fetchHostDiagnostics,
  fetchHostToolStatus,
  fetchHosts,
  getHostCapabilities,
  rejectHost,
} from '../api/hosts';
import type { DiscoveryConfirm, HostCreate } from '../types';
import { useEventStreamStatus } from '../context/EventStreamContext';

export function useHosts() {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: ['hosts'],
    queryFn: fetchHosts,
    refetchInterval: connected ? 60_000 : 15_000,
  });
}

export function useHost(id: string) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: ['host', id],
    queryFn: () => fetchHost(id),
    refetchInterval: connected ? 60_000 : 10_000,
  });
}

export function useHostDiagnostics(id: string) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: ['host-diagnostics', id],
    queryFn: () => fetchHostDiagnostics(id),
    refetchInterval: connected ? 60_000 : 10_000,
    enabled: !!id,
  });
}

export function useHostResourceTelemetry(id: string) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: ['host-resource-telemetry', id],
    queryFn: () => fetchHostResourceTelemetry(id),
    refetchInterval: connected ? 60_000 : 30_000,
    enabled: !!id,
  });
}

export function useHostToolStatus(id: string, enabled = true) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: ['host-tools-status', id],
    queryFn: () => fetchHostToolStatus(id),
    refetchInterval: connected ? 60_000 : 15_000,
    enabled: !!id && enabled,
  });
}

export function useCreateHost() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: HostCreate) => createHost(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['hosts'] }),
  });
}

export function useDeleteHost() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteHost(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['hosts'] }),
  });
}

export function useDiscoverDevices() {
  return useMutation({
    mutationFn: (hostId: string) => discoverDevices(hostId),
  });
}

export function useIntakeCandidates(hostId: string | null) {
  return useQuery({
    queryKey: ['intake-candidates', hostId],
    queryFn: () => fetchIntakeCandidates(hostId!),
    enabled: !!hostId,
  });
}

export function useConfirmDiscovery() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ hostId, body }: { hostId: string; body: DiscoveryConfirm }) =>
      confirmDiscovery(hostId, body),
    onSuccess: (_data, { hostId }) => {
      qc.invalidateQueries({ queryKey: ['hosts'] });
      qc.invalidateQueries({ queryKey: ['host', hostId] });
      qc.invalidateQueries({ queryKey: ['devices'] });
    },
  });
}

export function useApproveHost() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => approveHost(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['hosts'] });
      qc.invalidateQueries({ queryKey: ['host'] });
    },
  });
}

export function useRejectHost() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => rejectHost(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['hosts'] });
    },
  });
}

export function useHostCapabilities() {
  return useQuery({ queryKey: ['host-capabilities'], queryFn: getHostCapabilities, staleTime: 60_000 });
}
