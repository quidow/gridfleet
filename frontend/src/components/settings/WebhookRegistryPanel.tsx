import { useState } from 'react';
import { ChevronDown, ChevronUp, Loader2, Pencil, Play, RotateCcw, Trash2 } from 'lucide-react';
import ConfirmDialog from '../ui/ConfirmDialog';
import { LoadingSpinner } from '../LoadingSpinner';
import Modal from '../ui/Modal';
import { useEventCatalog } from '../../hooks/useEventCatalog';
import { useRetryWebhookDelivery, useWebhookDeliveries, useWebhooks } from '../../hooks/useWebhooks';
import { useWebhookAdmin } from './useWebhookAdmin';
import type { WebhookRead } from '../../types';
import SettingsPanelLayout from './SettingsPanelLayout';

export default function WebhookRegistryPanel() {
  const { data: webhooks, isLoading: webhooksLoading } = useWebhooks();
  const { data: eventCatalog, isLoading: eventCatalogLoading } = useEventCatalog();
  const admin = useWebhookAdmin();

  if (webhooksLoading || eventCatalogLoading) {
    return <LoadingSpinner />;
  }

  const eventTypeOptions = eventCatalog?.map((event) => event.name) ?? [];

  return (
    <SettingsPanelLayout
      title="Webhooks"
      description="Receive HTTP callbacks when device or session events occur."
      actions={
        <button
          onClick={admin.openCreateWebhook}
          className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-on hover:bg-accent-hover"
        >
          Add Webhook
        </button>
      }
    >

      {!webhooks?.length ? (
        <p className="py-12 text-center text-text-3">No webhooks configured. Add one to receive event notifications.</p>
      ) : (
        <div className="overflow-hidden rounded-lg bg-surface-1 shadow">
          <table className="min-w-full divide-y divide-border">
            <thead className="bg-surface-2">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase text-text-3">Name</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase text-text-3">URL</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase text-text-3">Events</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase text-text-3">Enabled</th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase text-text-3">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {webhooks.map((webhook) => (
                <WebhookRow
                  key={webhook.id}
                  webhook={webhook}
                  onDelete={() => admin.setDeleteWebhookTarget(webhook)}
                  onEdit={() => admin.openEditWebhook(webhook)}
                  onTest={() => admin.handleTestWebhook(webhook)}
                  onToggleEnabled={() => admin.handleToggleEnabled(webhook)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      <Modal
        isOpen={admin.showWebhookModal}
        onClose={() => admin.setShowWebhookModal(false)}
        title={admin.editingWebhookId ? 'Edit Webhook' : 'Add Webhook'}
      >
        <form onSubmit={admin.handleWebhookSubmit} className="space-y-4">
          <div>
            <label className="mb-1 block text-sm font-medium text-text-2">Name</label>
            <input
              value={admin.webhookForm.name}
              onChange={(event) => admin.setWebhookForm({ ...admin.webhookForm, name: event.target.value })}
              required
              className="w-full rounded-md border border-border-strong px-3 py-2 text-sm"
              placeholder="e.g. Slack #device-alerts"
            />
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium text-text-2">URL</label>
            <input
              value={admin.webhookForm.url}
              onChange={(event) => admin.setWebhookForm({ ...admin.webhookForm, url: event.target.value })}
              required
              type="url"
              className="w-full rounded-md border border-border-strong px-3 py-2 text-sm"
              placeholder="https://hooks.slack.com/services/..."
            />
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium text-text-2">Event Types</label>
            <div className="mt-1 grid grid-cols-2 gap-2">
              {eventTypeOptions.map((eventType) => (
                <label key={eventType} className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={admin.webhookForm.event_types.includes(eventType)}
                    onChange={() => admin.toggleEventType(eventType)}
                    className="rounded border-border-strong"
                  />
                  {eventType}
                </label>
              ))}
            </div>
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button type="button" onClick={() => admin.setShowWebhookModal(false)} className="px-4 py-2 text-sm text-text-2 hover:text-text-1">
              Cancel
            </button>
            <button type="submit" className="rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-on hover:bg-accent-hover">
              {admin.editingWebhookId ? 'Save' : 'Create'}
            </button>
          </div>
        </form>
      </Modal>

      <ConfirmDialog
        isOpen={!!admin.deleteWebhookTarget}
        title="Delete Webhook"
        message={`Are you sure you want to delete "${admin.deleteWebhookTarget?.name}"?`}
        variant="danger"
        confirmLabel="Delete"
        onConfirm={async () => {
          if (admin.deleteWebhookTarget) {
            await admin.deleteWebhookMut.mutateAsync(admin.deleteWebhookTarget.id);
          }
          admin.setDeleteWebhookTarget(null);
        }}
        onClose={() => admin.setDeleteWebhookTarget(null)}
      />
    </SettingsPanelLayout>
  );
}

function WebhookRow({
  webhook,
  onDelete,
  onEdit,
  onTest,
  onToggleEnabled,
}: {
  webhook: WebhookRead;
  onDelete: () => void;
  onEdit: () => void;
  onTest: () => void;
  onToggleEnabled: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const { data, isLoading } = useWebhookDeliveries(webhook.id, expanded);
  const retryMut = useRetryWebhookDelivery(webhook.id);

  return (
    <>
      <tr className="hover:bg-surface-2">
        <td className="px-4 py-3 text-sm font-medium text-text-1">{webhook.name}</td>
        <td className="max-w-xs truncate px-4 py-3 text-sm text-text-3" title={webhook.url}>
          {webhook.url}
        </td>
        <td className="px-4 py-3">
          <div className="flex flex-wrap gap-1">
            {webhook.event_types.map((eventType) => (
              <span key={eventType} className="inline-block rounded bg-surface-2 px-1.5 py-0.5 text-xs text-text-2">
                {eventType}
              </span>
            ))}
          </div>
        </td>
        <td className="px-4 py-3">
          <button
            onClick={onToggleEnabled}
            className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${
              webhook.enabled ? 'bg-accent' : 'bg-border-strong'
            }`}
          >
            <span
              className={`inline-block h-3.5 w-3.5 rounded-full bg-surface-1 transition-transform ${
                webhook.enabled ? 'translate-x-4.5' : 'translate-x-0.5'
              }`}
            />
          </button>
        </td>
        <td className="px-4 py-3">
          <div className="flex items-center gap-2">
            <button onClick={onEdit} className="text-text-3 hover:text-accent-hover" title="Edit">
              <Pencil size={16} />
            </button>
            <button onClick={onTest} className="text-text-3 hover:text-success-foreground" title="Send test event">
              <Play size={16} />
            </button>
            <button onClick={onDelete} className="text-text-3 hover:text-danger-foreground" title="Delete">
              <Trash2 size={16} />
            </button>
            <button
              onClick={() => setExpanded((value) => !value)}
              className="inline-flex items-center gap-1 rounded border border-border px-2 py-1 text-xs font-medium text-text-2 hover:border-border-strong hover:text-text-1"
            >
              Recent Deliveries
              {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
            </button>
          </div>
        </td>
      </tr>
      {expanded ? (
        <tr className="bg-surface-2/70">
          <td colSpan={5} className="px-4 py-4">
            {isLoading ? (
              <div className="flex items-center gap-2 text-sm text-text-3">
                <Loader2 className="animate-spin text-text-3" size={16} />
                <span>Loading recent deliveries…</span>
              </div>
            ) : !data?.items.length ? (
              <p className="text-sm text-text-3">No deliveries recorded yet.</p>
            ) : (
              <div className="space-y-3">
                {data.items.map((delivery) => (
                  <div
                    key={delivery.id}
                    className="flex flex-col gap-2 rounded-lg border border-border bg-surface-1 px-3 py-3 text-sm text-text-2 md:flex-row md:items-start md:justify-between"
                  >
                    <div className="space-y-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-medium text-text-1">{delivery.event_type}</span>
                        <StatusPill status={delivery.status} />
                        <span className="text-xs text-text-3">
                          Attempt {delivery.attempts} of {delivery.max_attempts}
                        </span>
                      </div>
                      <p className="text-xs text-text-3">
                        {formatTimestamp(delivery.last_attempt_at ?? delivery.created_at)}
                      </p>
                      {delivery.last_error ? <p className="text-sm text-danger-foreground">{delivery.last_error}</p> : null}
                    </div>
                    {delivery.status === 'failed' || delivery.status === 'exhausted' ? (
                      <button
                        onClick={() => retryMut.mutate(delivery.id)}
                        className="inline-flex items-center gap-1 self-start rounded border border-border px-2 py-1 text-xs font-medium text-text-2 hover:border-accent hover:text-accent-hover"
                      >
                        <RotateCcw size={14} />
                        Retry
                      </button>
                    ) : null}
                  </div>
                ))}
                {data.total > data.items.length ? (
                  <p className="text-xs text-text-3">Showing {data.items.length} of {data.total} deliveries.</p>
                ) : null}
              </div>
            )}
          </td>
        </tr>
      ) : null}
    </>
  );
}

function StatusPill({ status }: { status: string }) {
  const className =
    status === 'delivered'
      ? 'bg-success-soft text-success-foreground'
      : status === 'pending'
        ? 'bg-warning-soft text-warning-foreground'
        : 'bg-danger-soft text-danger-foreground';
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${className}`}>{status}</span>;
}

function formatTimestamp(value: string) {
  return new Date(value).toLocaleString();
}
