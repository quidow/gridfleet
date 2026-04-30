import api from './client';
import type { WebhookCreate, WebhookDeliveryListRead, WebhookDeliveryRead, WebhookRead, WebhookUpdate } from '../types';

export async function fetchWebhooks(): Promise<WebhookRead[]> {
  const { data } = await api.get('/webhooks');
  return data;
}


export async function createWebhook(body: WebhookCreate): Promise<WebhookRead> {
  const { data } = await api.post('/webhooks', body);
  return data;
}

export async function updateWebhook(id: string, body: WebhookUpdate): Promise<WebhookRead> {
  const { data } = await api.patch(`/webhooks/${id}`, body);
  return data;
}

export async function deleteWebhook(id: string): Promise<void> {
  await api.delete(`/webhooks/${id}`);
}

export async function testWebhook(id: string): Promise<{ status: string }> {
  const { data } = await api.post(`/webhooks/${id}/test`);
  return data;
}

export async function fetchWebhookDeliveries(id: string, limit = 10): Promise<WebhookDeliveryListRead> {
  const { data } = await api.get(`/webhooks/${id}/deliveries`, { params: { limit } });
  return data;
}

export async function retryWebhookDelivery(id: string, deliveryId: string): Promise<WebhookDeliveryRead> {
  const { data } = await api.post(`/webhooks/${id}/deliveries/${deliveryId}/retry`);
  return data;
}
