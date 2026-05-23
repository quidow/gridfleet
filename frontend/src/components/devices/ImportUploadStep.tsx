import { useState } from 'react';
import { SectionHeader } from '../ui/SectionHeader';
import type { ExportBundle } from '../../api/devicesPortability';

interface Props {
  onBundle: (bundle: ExportBundle) => void;
}

export function ImportUploadStep({ onBundle }: Props) {
  const [error, setError] = useState<string | null>(null);

  const handleFile = async (file: File): Promise<void> => {
    setError(null);
    try {
      const text = await file.text();
      const parsed = JSON.parse(text) as ExportBundle;
      if (parsed.schema_version !== 1) {
        setError(`unsupported schema_version: ${parsed.schema_version}`);
        return;
      }
      onBundle(parsed);
    } catch (e) {
      setError(`could not parse JSON: ${(e as Error).message}`);
    }
  };

  return (
    <div className="space-y-3">
      <SectionHeader
        level={3}
        title="Step 1 · Upload bundle"
        description="Select a portability bundle exported from another GridFleet instance."
      />
      <label className="grid gap-1 text-sm" htmlFor="import-bundle">
        <span className="font-medium text-text-2">Device bundle (JSON)</span>
        <input
          id="import-bundle"
          type="file"
          accept="application/json"
          aria-label="bundle"
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) void handleFile(file);
          }}
          className="rounded border border-border bg-surface-2 px-2 py-1 text-sm text-text-1 file:mr-2 file:rounded file:border-0 file:bg-accent file:px-2 file:py-0.5 file:text-xs file:font-medium file:text-accent-on hover:file:bg-accent-hover"
        />
      </label>
      {error && (
        <p
          role="alert"
          className="rounded border border-danger-foreground bg-danger-soft px-3 py-2 text-sm text-danger-foreground"
        >
          {error}
        </p>
      )}
    </div>
  );
}
