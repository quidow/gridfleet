import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  deleteDriverPack,
  fetchDriverPack,
  fetchDriverPackHosts,
  fetchDriverPackReleases,
  setDriverPackCurrentRelease,
} from '../api/driverPackDetail';

export function useDriverDetail(packId: string) {
  return useQuery({
    queryKey: ['driver-pack', packId],
    queryFn: () => fetchDriverPack(packId),
    enabled: packId.length > 0,
    refetchInterval: 15_000,
  });
}

export function useDriverReleases(packId: string) {
  return useQuery({
    queryKey: ['driver-pack-releases', packId],
    queryFn: () => fetchDriverPackReleases(packId),
    enabled: packId.length > 0,
    refetchInterval: 15_000,
  });
}

export function useDriverPackHosts(packId: string) {
  return useQuery({
    queryKey: ['driver-pack-hosts', packId],
    queryFn: () => fetchDriverPackHosts(packId),
    enabled: packId.length > 0,
    refetchInterval: 15_000,
  });
}

export function useDeleteDriverPack() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (packId: string) => deleteDriverPack(packId),
    onSuccess: (_data, packId) => {
      void qc.invalidateQueries({ queryKey: ['driver-pack-catalog'] });
      void qc.removeQueries({ queryKey: ['driver-pack', packId] });
      void qc.removeQueries({ queryKey: ['driver-pack-releases', packId] });
      void qc.removeQueries({ queryKey: ['driver-pack-hosts', packId] });
    },
  });
}

export function useSetDriverPackCurrentRelease() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ packId, release }: { packId: string; release: string }) =>
      setDriverPackCurrentRelease(packId, release),
    onSuccess: (_data, variables) => {
      void qc.invalidateQueries({ queryKey: ['driver-pack', variables.packId] });
      void qc.invalidateQueries({ queryKey: ['driver-pack-releases', variables.packId] });
      void qc.invalidateQueries({ queryKey: ['driver-pack-hosts', variables.packId] });
      void qc.invalidateQueries({ queryKey: ['driver-pack-catalog'] });
    },
  });
}
