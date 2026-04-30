import { useContext } from 'react';
import { QueryClient, QueryClientContext, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  fetchDriverPackCatalog,
  fetchHostDriverPacks,
  setDriverPackPolicy,
  setDriverPackState,
} from '../api/driverPacks';
import type { DriverPack, RuntimePolicy } from '../types/driverPacks';

export function platformKey(packId: string, platformId: string): string {
  return `${packId}:${platformId}`;
}

export function buildPlatformLabelMap(packs: DriverPack[]): Map<string, string> {
  const labels = new Map<string, string>();
  for (const pack of packs) {
    for (const platform of pack.platforms ?? []) {
      labels.set(platformKey(pack.id, platform.id), platform.display_name);
    }
  }
  return labels;
}

/** Build a map of platform_id → display_name (first pack wins on collision). */
export function buildPlatformIdLabelMap(packs: DriverPack[]): Map<string, string> {
  const labels = new Map<string, string>();
  for (const pack of packs) {
    for (const platform of pack.platforms ?? []) {
      if (!labels.has(platform.id)) {
        labels.set(platform.id, platform.display_name);
      }
    }
  }
  return labels;
}

const fallbackQueryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: false,
    },
  },
});

export function useDriverPackCatalog() {
  const contextClient = useContext(QueryClientContext);
  return useQuery({
    queryKey: ['driver-pack-catalog'],
    queryFn: fetchDriverPackCatalog,
    refetchInterval: 5000,
  }, contextClient ?? fallbackQueryClient);
}

export function usePlatformLabelMap(): Map<string, string> {
  const { data } = useDriverPackCatalog();
  return data ? buildPlatformLabelMap(data) : new Map<string, string>();
}

/** Returns a map of platform_id → display_name from the catalog. */
export function usePlatformIdLabelMap(): Map<string, string> {
  const { data } = useDriverPackCatalog();
  return data ? buildPlatformIdLabelMap(data) : new Map<string, string>();
}

export function useSetDriverPackState() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ packId, state }: { packId: string; state: 'enabled' | 'disabled' }) =>
      setDriverPackState(packId, state),
    onSuccess: (_data, variables) => {
      void qc.invalidateQueries({ queryKey: ['driver-pack-catalog'] });
      void qc.invalidateQueries({ queryKey: ['driver-pack', variables.packId] });
    },
  });
}

export function useSetDriverPackPolicy() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ packId, runtimePolicy }: { packId: string; runtimePolicy: RuntimePolicy }) =>
      setDriverPackPolicy(packId, runtimePolicy),
    onSuccess: (_data, variables) => {
      void qc.invalidateQueries({ queryKey: ['driver-pack-catalog'] });
      void qc.invalidateQueries({ queryKey: ['driver-pack', variables.packId] });
    },
  });
}

export function useHostDriverPacks(hostId: string) {
  return useQuery({
    queryKey: ['host-driver-packs', hostId],
    queryFn: () => fetchHostDriverPacks(hostId),
    enabled: !!hostId,
    refetchInterval: 5000,
  });
}
