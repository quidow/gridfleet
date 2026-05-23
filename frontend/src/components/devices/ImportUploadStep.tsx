import { useState } from "react";

import type { ExportBundle } from "../../api/devicesPortability";

interface Props {
  onBundle: (bundle: ExportBundle) => void;
}

export function ImportUploadStep({ onBundle }: Props): JSX.Element {
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
      <label className="block text-sm font-medium" htmlFor="import-bundle">
        Device bundle (JSON)
      </label>
      <input
        id="import-bundle"
        type="file"
        accept="application/json"
        aria-label="bundle"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) void handleFile(file);
        }}
      />
      {error && <p className="text-sm text-red-600">{error}</p>}
    </div>
  );
}
