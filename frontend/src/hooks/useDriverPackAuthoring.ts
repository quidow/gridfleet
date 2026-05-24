import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { createFromTemplate, fetchTemplates, forkDriverPack } from '../api/driverPackAuthoring';

export function useTemplates() {
  return useQuery({
    queryKey: ['driver-pack-templates'],
    queryFn: fetchTemplates,
    staleTime: Infinity,
  });
}

export function useCreateFromTemplate() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      templateId,
      body,
    }: {
      templateId: string;
      body: { pack_id: string; release: string; display_name?: string };
    }) => createFromTemplate(templateId, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['driver-pack-catalog'] });
    },
  });
}

export function useForkDriverPack() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      sourcePackId,
      body,
    }: {
      sourcePackId: string;
      body: { new_pack_id: string; display_name?: string };
    }) => forkDriverPack(sourcePackId, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['driver-pack-catalog'] });
    },
  });
}
