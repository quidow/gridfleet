import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { bulkUpdateSettings, fetchSettings, resetAllSettings, resetSetting } from '../api/settings';

const SETTINGS_POLL_MS = 60_000;

export function useSettings() {
  return useQuery({
    queryKey: ['settings'],
    queryFn: fetchSettings,
    refetchInterval: SETTINGS_POLL_MS,
    staleTime: SETTINGS_POLL_MS / 2,
  });
}

export function useBulkUpdateSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (settings: Record<string, unknown>) => bulkUpdateSettings(settings),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['settings'] }),
  });
}

export function useResetSetting() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (key: string) => resetSetting(key),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['settings'] }),
  });
}

export function useResetAllSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => resetAllSettings(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['settings'] }),
  });
}
