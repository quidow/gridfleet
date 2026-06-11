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
import { qk } from '../lib/queryKeys';

const PLUGINS_POLL_MS = 30_000;

export function usePlugins() {
  return useQuery({
    queryKey: qk.plugins.root,
    queryFn: fetchPlugins,
    refetchInterval: PLUGINS_POLL_MS,
    staleTime: PLUGINS_POLL_MS / 2,
  });
}

export function useCreatePlugin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: AppiumPluginCreate) => createPlugin(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.plugins.root }),
  });
}

export function useUpdatePlugin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: AppiumPluginUpdate }) => updatePlugin(id, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.plugins.root }),
  });
}

export function useDeletePlugin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deletePlugin(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.plugins.root }),
  });
}

export function useHostPlugins(hostId: string) {
  return useQuery({
    queryKey: qk.hostPlugins.byHost(hostId),
    queryFn: () => fetchHostPlugins(hostId),
    enabled: !!hostId,
    refetchInterval: PLUGINS_POLL_MS,
    staleTime: PLUGINS_POLL_MS / 2,
  });
}

export function useSyncHostPlugins() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (hostId: string) => syncHostPlugins(hostId),
    onSuccess: (_data, hostId) => qc.invalidateQueries({ queryKey: qk.hostPlugins.byHost(hostId) }),
  });
}

export function useSyncAllPlugins() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => syncAllPlugins(),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: qk.hostPlugins.root });
    },
  });
}
