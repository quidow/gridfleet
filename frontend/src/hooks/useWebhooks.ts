import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  createWebhook,
  deleteWebhook,
  fetchWebhookDeliveries,
  fetchWebhooks,
  retryWebhookDelivery,
  testWebhook,
  updateWebhook,
} from '../api/webhooks';
import type { WebhookCreate, WebhookUpdate } from '../types';

export function useWebhooks() {
  return useQuery({
    queryKey: ['webhooks'],
    queryFn: fetchWebhooks,
  });
}

export function useCreateWebhook() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: WebhookCreate) => createWebhook(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['webhooks'] }),
  });
}

export function useUpdateWebhook() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: WebhookUpdate }) => updateWebhook(id, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['webhooks'] }),
  });
}

export function useDeleteWebhook() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteWebhook(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['webhooks'] }),
  });
}

export function useTestWebhook() {
  return useMutation({
    mutationFn: (id: string) => testWebhook(id),
  });
}

export function useWebhookDeliveries(id: string, enabled = true, limit = 10) {
  return useQuery({
    queryKey: ['webhooks', id, 'deliveries', limit],
    queryFn: () => fetchWebhookDeliveries(id, limit),
    enabled,
  });
}

export function useRetryWebhookDelivery(id: string, limit = 10) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (deliveryId: string) => retryWebhookDelivery(id, deliveryId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['webhooks', id, 'deliveries', limit] }),
  });
}
