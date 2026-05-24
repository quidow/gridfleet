import { useState } from 'react';
import { Plus, Trash2 } from 'lucide-react';
import { useHostToolEnv, useUpdateHostToolEnv } from '../../hooks/useHostToolEnv';
import { Button } from '../ui/Button';
import { Card } from '../ui/Card';
import { ConfirmDialog } from '../ui/ConfirmDialog';
import { FetchError } from '../ui/FetchError';

type Props = {
  hostId: string;
};

type EnvRow = {
  id: string;
  key: string;
  value: string;
};

function envRecordToRows(record: Record<string, string>): EnvRow[] {
  return Object.entries(record).map(([key, value]) => ({ id: crypto.randomUUID(), key, value }));
}

function rowsToEnvRecord(rows: EnvRow[]): Record<string, string> {
  const result: Record<string, string> = {};
  for (const row of rows) {
    const trimmed = row.key.trim();
    if (trimmed) {
      result[trimmed] = row.value;
    }
  }
  return result;
}

export function HostToolEnvPanel({ hostId }: Props) {
  const { data: rawData, isLoading, isError, refetch } = useHostToolEnv(hostId);
  const envData = rawData?.env;
  const updateMutation = useUpdateHostToolEnv(hostId);

  const [syncedData, setSyncedData] = useState<Record<string, string> | undefined>(undefined);
  const [rows, setRows] = useState<EnvRow[]>([]);
  const [dirty, setDirty] = useState(false);
  const [pendingDeleteIndex, setPendingDeleteIndex] = useState<number | null>(null);

  if (envData !== undefined && envData !== syncedData && !dirty) {
    setSyncedData(envData);
    setRows(envRecordToRows(envData));
    setDirty(false);
  }

  function handleKeyChange(index: number, newKey: string) {
    setRows((prev) => prev.map((row, i) => (i === index ? { ...row, key: newKey } : row)));
    setDirty(true);
  }

  function handleValueChange(index: number, newValue: string) {
    setRows((prev) => prev.map((row, i) => (i === index ? { ...row, value: newValue } : row)));
    setDirty(true);
  }

  function handleAddRow() {
    setRows((prev) => [...prev, { id: crypto.randomUUID(), key: '', value: '' }]);
    setDirty(true);
  }

  function handleDeleteRow(index: number) {
    const row = rows[index];
    if (row.key.trim()) {
      setPendingDeleteIndex(index);
    } else {
      setRows((prev) => prev.filter((_, i) => i !== index));
      setDirty(true);
    }
  }

  function confirmDelete() {
    if (pendingDeleteIndex === null) return;
    const newRows = rows.filter((_, i) => i !== pendingDeleteIndex);
    setRows(newRows);
    setPendingDeleteIndex(null);
    updateMutation.mutate(rowsToEnvRecord(newRows), {
      onSuccess: () => setDirty(false),
    });
  }

  function handleSave() {
    updateMutation.mutate(rowsToEnvRecord(rows), {
      onSuccess: () => {
        setDirty(false);
      },
    });
  }

  return (
    <Card padding="none">
      <div className="flex items-center justify-between border-b border-border px-5 py-4">
        <h2 className="text-sm font-medium text-text-2">Tool Environment Variables</h2>
        <div className="flex items-center gap-2">
          <Button variant="secondary" size="sm" leadingIcon={<Plus size={14} />} onClick={handleAddRow}>
            Add Variable
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={handleSave}
            disabled={!dirty}
            loading={updateMutation.isPending}
          >
            Save
          </Button>
        </div>
      </div>

      {isError ? (
        <div className="px-5 py-4">
          <FetchError message="Failed to load environment variables." onRetry={() => refetch()} />
        </div>
      ) : isLoading ? (
        <p className="px-5 py-8 text-center text-sm text-text-3">Loading environment variables...</p>
      ) : rows.length === 0 ? (
        <div className="px-5 py-8 text-center">
          <p className="text-sm text-text-2">No tool environment variables configured.</p>
          <p className="mt-1 text-xs text-text-3">
            Add variables like <span className="font-mono">ANDROID_HOME</span> to override the environment for Appium
            subprocesses on this host.
          </p>
        </div>
      ) : (
        <div>
          <div className="grid grid-cols-[1fr_1fr_auto] gap-0 border-b border-border px-5 py-2">
            <span className="text-xs font-medium uppercase text-text-3">Name</span>
            <span className="text-xs font-medium uppercase text-text-3">Value</span>
            <span className="w-8" />
          </div>
          <div className="divide-y divide-border">
            {rows.map((row, index) => {
              const trimmedKey = row.key.trim();
              const isDuplicate = trimmedKey !== '' && rows.some((r, i) => i !== index && r.key.trim() === trimmedKey);
              return (
              <div key={row.id} className="grid grid-cols-[1fr_1fr_auto] items-center gap-2 px-5 py-2">
                <div>
                  <input
                    type="text"
                    value={row.key}
                    onChange={(e) => handleKeyChange(index, e.target.value)}
                    placeholder="VARIABLE_NAME"
                    className={`w-full rounded border bg-surface-2 px-2 py-1.5 font-mono text-sm text-text-1 placeholder-text-3 focus:outline-none focus:ring-1 ${isDuplicate ? 'border-danger-foreground focus:border-danger-foreground focus:ring-danger-foreground' : 'border-border focus:border-accent focus:ring-accent'}`}
                    spellCheck={false}
                  />
                  {isDuplicate && <p className="mt-0.5 text-xs text-danger-foreground">Duplicate name</p>}
                </div>
                <input
                  type="text"
                  value={row.value}
                  onChange={(e) => handleValueChange(index, e.target.value)}
                  placeholder="/path/to/value"
                  className="w-full rounded border border-border bg-surface-2 px-2 py-1.5 font-mono text-sm text-text-1 placeholder-text-3 focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent"
                  spellCheck={false}
                />
                <button
                  type="button"
                  onClick={() => handleDeleteRow(index)}
                  aria-label="Remove variable"
                  className="flex h-7 w-7 items-center justify-center rounded text-text-3 transition-colors hover:bg-surface-2 hover:text-danger-foreground"
                >
                  <Trash2 size={14} />
                </button>
              </div>
            );})}
          </div>
        </div>
      )}

      {updateMutation.isError ? (
        <p className="border-t border-border px-5 py-3 text-sm text-danger-foreground">
          Failed to save environment variables. Please try again.
        </p>
      ) : null}

      <ConfirmDialog
        isOpen={pendingDeleteIndex !== null}
        onClose={() => setPendingDeleteIndex(null)}
        onConfirm={confirmDelete}
        title="Remove variable"
        message={
          pendingDeleteIndex !== null
            ? `Remove "${rows[pendingDeleteIndex]?.key}" from this host's environment?`
            : ''
        }
        confirmLabel="Remove"
        variant="danger"
      />
    </Card>
  );
}
