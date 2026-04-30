import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  createPlugin,
  deletePlugin,
  fetchHostPlugins,
  fetchPlugins,
  syncAllPlugins,
  syncHostPlugins,
  updatePlugin,
} from '../api/plugins';
import type { AppiumPluginCreate, AppiumPluginUpdate } from '../types';

export function usePlugins() {
  return useQuery({
    queryKey: ['plugins'],
    queryFn: fetchPlugins,
  });
}

export function useCreatePlugin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: AppiumPluginCreate) => createPlugin(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['plugins'] }),
  });
}

export function useUpdatePlugin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: AppiumPluginUpdate }) => updatePlugin(id, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['plugins'] }),
  });
}

export function useDeletePlugin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deletePlugin(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['plugins'] }),
  });
}

export function useHostPlugins(hostId: string) {
  return useQuery({
    queryKey: ['host-plugins', hostId],
    queryFn: () => fetchHostPlugins(hostId),
    enabled: !!hostId,
  });
}

export function useSyncHostPlugins() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (hostId: string) => syncHostPlugins(hostId),
    onSuccess: (_data, hostId) => qc.invalidateQueries({ queryKey: ['host-plugins', hostId] }),
  });
}

export function useSyncAllPlugins() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => syncAllPlugins(),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ['host-plugins'] });
    },
  });
}
