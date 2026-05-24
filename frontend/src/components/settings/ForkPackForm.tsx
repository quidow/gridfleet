import { useState } from 'react';
import { Button, Card, Field, Select, TextField } from '../ui';
import { useDriverPackCatalog } from '../../hooks/useDriverPacks';
import { useForkDriverPack } from '../../hooks/useDriverPackAuthoring';

interface ForkPackFormProps {
  onSuccess: (packId: string) => void;
  onClose: () => void;
}

export function ForkPackForm({ onSuccess, onClose }: ForkPackFormProps) {
  const { data: catalog } = useDriverPackCatalog();
  const forkMutation = useForkDriverPack();

  const [sourcePackId, setSourcePackId] = useState('');
  const [newPackId, setNewPackId] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [error, setError] = useState<string | null>(null);

  const packOptions = (catalog ?? []).map((pack) => ({
    value: pack.id,
    label: `${pack.display_name} (${pack.id})`,
  }));

  const canSubmit = sourcePackId.length > 0 && newPackId.length > 0 && !forkMutation.isPending;

  function handleSubmit() {
    if (!canSubmit) return;
    setError(null);
    forkMutation.mutate(
      {
        sourcePackId,
        body: {
          new_pack_id: newPackId,
          display_name: displayName || undefined,
        },
      },
      {
        onSuccess: () => onSuccess(newPackId),
        onError: (err: unknown) => {
          setError(err instanceof Error ? err.message : 'Failed to fork driver pack.');
        },
      },
    );
  }

  return (
    <div className="grid gap-4">
      <Card padding="md" className="grid gap-3">
        <Field label="Source Pack" htmlFor="fork-source" required>
          <Select
            id="fork-source"
            value={sourcePackId}
            onChange={setSourcePackId}
            options={packOptions}
            placeholder="Select a driver pack…"
            size="sm"
            fullWidth
          />
        </Field>
        <Field label="New Pack ID" htmlFor="fork-new-id" required>
          <TextField
            id="fork-new-id"
            value={newPackId}
            onChange={setNewPackId}
            size="sm"
            placeholder="e.g. my-custom-uiautomator2"
          />
        </Field>
        <Field label="Display Name" htmlFor="fork-display-name" hint="Optional">
          <TextField
            id="fork-display-name"
            value={displayName}
            onChange={setDisplayName}
            size="sm"
          />
        </Field>
      </Card>

      {error !== null && (
        <p role="alert" className="rounded border border-danger-foreground bg-danger-soft px-3 py-2 text-sm text-danger-foreground">
          {error}
        </p>
      )}

      <div className="flex gap-2">
        <Button size="sm" onClick={handleSubmit} disabled={!canSubmit}>
          {forkMutation.isPending ? 'Forking…' : 'Fork Driver Pack'}
        </Button>
        <Button size="sm" variant="secondary" onClick={onClose}>
          Cancel
        </Button>
      </div>
    </div>
  );
}
