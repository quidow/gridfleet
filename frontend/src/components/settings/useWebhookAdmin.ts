import { type FormEvent, useState } from 'react';
import { toast } from 'sonner';
import { useCreateWebhook, useDeleteWebhook, useTestWebhook, useUpdateWebhook } from '../../hooks/useWebhooks';
import type { WebhookCreate, WebhookRead, WebhookUpdate } from '../../types';

const EMPTY_WEBHOOK_FORM: WebhookCreate = {
  name: '',
  url: '',
  event_types: [],
  enabled: true,
};

export function useWebhookAdmin() {
  const createWebhookMut = useCreateWebhook();
  const updateWebhookMut = useUpdateWebhook();
  const deleteWebhookMut = useDeleteWebhook();
  const testWebhookMut = useTestWebhook();
  const [showWebhookModal, setShowWebhookModal] = useState(false);
  const [editingWebhookId, setEditingWebhookId] = useState<string | null>(null);
  const [webhookForm, setWebhookForm] = useState<WebhookCreate>(EMPTY_WEBHOOK_FORM);
  const [deleteWebhookTarget, setDeleteWebhookTarget] = useState<WebhookRead | null>(null);

  function openCreateWebhook() {
    setEditingWebhookId(null);
    setWebhookForm(EMPTY_WEBHOOK_FORM);
    setShowWebhookModal(true);
  }

  function openEditWebhook(webhook: WebhookRead) {
    setEditingWebhookId(webhook.id);
    setWebhookForm({
      name: webhook.name,
      url: webhook.url,
      event_types: webhook.event_types,
      enabled: webhook.enabled,
    });
    setShowWebhookModal(true);
  }

  function toggleEventType(type: string) {
    setWebhookForm((previous) => ({
      ...previous,
      event_types: previous.event_types.includes(type)
        ? previous.event_types.filter((eventType) => eventType !== type)
        : [...previous.event_types, type],
    }));
  }

  async function handleWebhookSubmit(event: FormEvent) {
    event.preventDefault();
    if (editingWebhookId) {
      const body: WebhookUpdate = {
        name: webhookForm.name,
        url: webhookForm.url,
        event_types: webhookForm.event_types,
        enabled: webhookForm.enabled,
      };
      await updateWebhookMut.mutateAsync({ id: editingWebhookId, body });
    } else {
      await createWebhookMut.mutateAsync(webhookForm);
    }
    setShowWebhookModal(false);
  }

  async function handleToggleEnabled(webhook: WebhookRead) {
    await updateWebhookMut.mutateAsync({ id: webhook.id, body: { enabled: !webhook.enabled } });
  }

  async function handleTestWebhook(webhook: WebhookRead) {
    try {
      await testWebhookMut.mutateAsync(webhook.id);
      toast.success(`Test event sent to "${webhook.name}"`);
    } catch {
      toast.error(`Failed to send test event to "${webhook.name}"`);
    }
  }

  return {
    createWebhookMut,
    deleteWebhookMut,
    deleteWebhookTarget,
    editingWebhookId,
    handleTestWebhook,
    handleToggleEnabled,
    handleWebhookSubmit,
    openCreateWebhook,
    openEditWebhook,
    setDeleteWebhookTarget,
    setShowWebhookModal,
    showWebhookModal,
    toggleEventType,
    updateWebhookMut,
    webhookForm,
    setWebhookForm,
  };
}
