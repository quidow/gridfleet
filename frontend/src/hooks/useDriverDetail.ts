import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  deleteDriverPack,
  deleteRelease,
  fetchDriverPack,
  fetchDriverPackHosts,
  fetchDriverPackReleases,
  setDriverPackCurrentRelease,
  updateRuntimePolicy,
} from '../api/driverPackDetail';
import type { RuntimePolicy } from '../types/driverPacks';
import { useEventStreamStatus } from '../context/EventStreamContext';
import { sseAdaptivePolling, POLL_RELAXED_MS } from './polling';
import { qk } from '../lib/queryKeys';

export function useDriverDetail(packId: string) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.driverPack.detail(packId),
    queryFn: () => fetchDriverPack(packId),
    enabled: packId.length > 0,
    ...sseAdaptivePolling(connected, POLL_RELAXED_MS),
  });
}

export function useDriverReleases(packId: string) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.driverPackReleases.byPack(packId),
    queryFn: () => fetchDriverPackReleases(packId),
    enabled: packId.length > 0,
    ...sseAdaptivePolling(connected, POLL_RELAXED_MS),
  });
}

export function useDriverPackHosts(packId: string) {
  const { connected } = useEventStreamStatus();
  return useQuery({
    queryKey: qk.driverPackHosts.byPack(packId),
    queryFn: () => fetchDriverPackHosts(packId),
    enabled: packId.length > 0,
    ...sseAdaptivePolling(connected, POLL_RELAXED_MS),
  });
}

export function useDeleteDriverPack() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (packId: string) => deleteDriverPack(packId),
    onSuccess: (_data, packId) => {
      void qc.invalidateQueries({ queryKey: qk.driverPackCatalog.root });
      void qc.removeQueries({ queryKey: qk.driverPack.detail(packId) });
      void qc.removeQueries({ queryKey: qk.driverPackReleases.byPack(packId) });
      void qc.removeQueries({ queryKey: qk.driverPackHosts.byPack(packId) });
    },
  });
}

export function useSetDriverPackCurrentRelease() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ packId, release }: { packId: string; release: string }) =>
      setDriverPackCurrentRelease(packId, release),
    onSuccess: (_data, variables) => {
      void qc.invalidateQueries({ queryKey: qk.driverPack.detail(variables.packId) });
      void qc.invalidateQueries({ queryKey: qk.driverPackReleases.byPack(variables.packId) });
      void qc.invalidateQueries({ queryKey: qk.driverPackHosts.byPack(variables.packId) });
      void qc.invalidateQueries({ queryKey: qk.driverPackCatalog.root });
    },
  });
}

export function useUpdateRuntimePolicy() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ packId, runtimePolicy }: { packId: string; runtimePolicy: RuntimePolicy }) =>
      updateRuntimePolicy(packId, runtimePolicy),
    onSuccess: (_data, variables) => {
      void qc.invalidateQueries({ queryKey: qk.driverPack.detail(variables.packId) });
      void qc.invalidateQueries({ queryKey: qk.driverPackCatalog.root });
    },
  });
}

export function useDeleteRelease() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ packId, release }: { packId: string; release: string }) =>
      deleteRelease(packId, release),
    onSuccess: (_data, variables) => {
      void qc.invalidateQueries({ queryKey: qk.driverPackReleases.byPack(variables.packId) });
      void qc.invalidateQueries({ queryKey: qk.driverPack.detail(variables.packId) });
      void qc.invalidateQueries({ queryKey: qk.driverPackCatalog.root });
    },
  });
}
