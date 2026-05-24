import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fetchHostToolEnv, updateHostToolEnv } from '../api/hosts';

export function useHostToolEnv(hostId: string) {
  return useQuery({
    queryKey: ['host-tool-env', hostId],
    queryFn: () => fetchHostToolEnv(hostId),
  });
}

export function useUpdateHostToolEnv(hostId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (env: Record<string, string>) => updateHostToolEnv(hostId, env),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['host-tool-env', hostId] });
    },
  });
}
