import { useState } from 'react';
import { Badge, Button, Card, Field, TextField } from '../ui';
import { useTemplates, useCreateFromTemplate } from '../../hooks/useDriverPackAuthoring';
import { LoadingSpinner } from '../LoadingSpinner';
import type { TemplateDescriptor } from '../../types/driverPacks';

interface TemplatePackFormProps {
  onSuccess: (packId: string) => void;
  onClose: () => void;
}

function TemplateCard({
  template,
  selected,
  onClick,
}: {
  template: TemplateDescriptor;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        'w-full rounded-lg border px-4 py-3 text-left transition',
        selected
          ? 'border-accent bg-accent/5 ring-2 ring-accent'
          : 'border-border hover:border-border-strong hover:bg-surface-2',
      ].join(' ')}
    >
      <div className="font-medium text-sm text-text-1">{template.display_name}</div>
      <p className="mt-1 text-xs text-text-3">{template.target_driver_summary}</p>
      {template.prerequisite_host_tools.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {template.prerequisite_host_tools.map((tool) => (
            <Badge key={tool} tone="neutral" size="sm">{tool}</Badge>
          ))}
        </div>
      )}
    </button>
  );
}

export function TemplatePackForm({ onSuccess, onClose }: TemplatePackFormProps) {
  const { data: templates, isLoading } = useTemplates();
  const createMutation = useCreateFromTemplate();

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [packId, setPackId] = useState('');
  const [release, setRelease] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [error, setError] = useState<string | null>(null);

  const selected = templates?.find((t) => t.template_id === selectedId) ?? null;

  function handleSelect(template: TemplateDescriptor) {
    setSelectedId(template.template_id);
    setPackId(template.source_pack_id);
    setRelease(new Date().getFullYear() + '.1');
    setDisplayName(template.display_name);
    setError(null);
  }

  const canSubmit = selectedId !== null && packId.length > 0 && release.length > 0 && !createMutation.isPending;

  function handleSubmit() {
    if (!selectedId || !canSubmit) return;
    setError(null);
    createMutation.mutate(
      {
        templateId: selectedId,
        body: {
          pack_id: packId,
          release,
          display_name: displayName || undefined,
        },
      },
      {
        onSuccess: () => onSuccess(packId),
        onError: (err: unknown) => {
          setError(err instanceof Error ? err.message : 'Failed to create driver pack.');
        },
      },
    );
  }

  if (isLoading) return <LoadingSpinner />;

  if (!templates || templates.length === 0) {
    return <p className="text-sm text-text-3">No templates available.</p>;
  }

  return (
    <div className="grid gap-4">
      <div className="grid gap-2">
        {templates.map((template) => (
          <TemplateCard
            key={template.template_id}
            template={template}
            selected={selectedId === template.template_id}
            onClick={() => handleSelect(template)}
          />
        ))}
      </div>

      {selected !== null && (
        <Card padding="md" className="grid gap-3">
          <Field label="Pack ID" htmlFor="template-pack-id" required>
            <TextField
              id="template-pack-id"
              value={packId}
              onChange={setPackId}
              size="sm"
              placeholder="e.g. appium-uiautomator2"
            />
          </Field>
          <Field label="Release" htmlFor="template-release" required>
            <TextField
              id="template-release"
              value={release}
              onChange={setRelease}
              size="sm"
              placeholder="e.g. 2026.1"
            />
          </Field>
          <Field label="Display Name" htmlFor="template-display-name" hint="Optional">
            <TextField
              id="template-display-name"
              value={displayName}
              onChange={setDisplayName}
              size="sm"
            />
          </Field>
        </Card>
      )}

      {error !== null && (
        <p role="alert" className="rounded border border-danger-foreground bg-danger-soft px-3 py-2 text-sm text-danger-foreground">
          {error}
        </p>
      )}

      <div className="flex gap-2">
        <Button size="sm" onClick={handleSubmit} disabled={!canSubmit}>
          {createMutation.isPending ? 'Creating…' : 'Create Driver Pack'}
        </Button>
        <Button size="sm" variant="secondary" onClick={onClose}>
          Cancel
        </Button>
      </div>
    </div>
  );
}
