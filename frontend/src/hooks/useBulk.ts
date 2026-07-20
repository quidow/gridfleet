import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  bulkStartNodes,
  bulkStopNodes,
  bulkRestartNodes,
  bulkDelete,
  bulkEnterMaintenance,
  bulkExitMaintenance,
  bulkReconnect,
} from '../api/bulk';
import type { BulkOperationResult } from '../types';
import { qk } from '../lib/queryKeys';

function useBulkMutation<T>(mutationFn: (body: T) => Promise<BulkOperationResult>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.devices.root });
      qc.invalidateQueries({ queryKey: qk.device.root });
    },
  });
}

export const useBulkStartNodes = () => useBulkMutation(bulkStartNodes);
export const useBulkStopNodes = () => useBulkMutation(bulkStopNodes);
export const useBulkRestartNodes = () => useBulkMutation(bulkRestartNodes);
export const useBulkDelete = () => useBulkMutation(bulkDelete);
export const useBulkEnterMaintenance = () => useBulkMutation(bulkEnterMaintenance);
export const useBulkExitMaintenance = () => useBulkMutation(bulkExitMaintenance);
export const useBulkReconnect = () => useBulkMutation(bulkReconnect);
