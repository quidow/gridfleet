import { useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';

import { uploadDriverPack } from '../../api/driverPackAuthoring';
import Card from '../ui/Card';

const DRIVER_PACK_ACCEPT = '.tar.gz,.tgz,.tar,.gz,application/gzip,application/x-gzip,application/x-tar';

interface UploadDriverPackFormProps {
  onSuccess?: () => void;
  onClose?: () => void;
}

export function UploadDriverPackForm({ onSuccess, onClose }: UploadDriverPackFormProps) {
  const [file, setFile] = useState<File | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const qc = useQueryClient();

  const upload = useMutation({
    mutationFn: () => uploadDriverPack(file!),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['driver-pack-catalog'] });
      onSuccess?.();
      onClose?.();
    },
    onError: (err: unknown) => {
      const message =
        err instanceof Error ? err.message : 'Upload failed. Please try again.';
      setErrorMessage(message);
    },
  });

  const canSubmit = file !== null && confirmed && !upload.isPending;

  function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const selected = e.target.files?.[0] ?? null;
    setFile(selected);
    setErrorMessage(null);
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setErrorMessage(null);
    upload.mutate();
  }

  return (
    <Card padding="md" className="grid gap-4">
      <section>
        <h3 className="font-semibold text-text-1">Upload driver pack archive</h3>
        <p className="mt-1 text-sm text-text-3">
          Upload a tarball (<code>.tar.gz</code> or <code>.tgz</code>) that includes a{' '}
          <code>manifest.yaml</code> and an optional adapter wheel. The adapter may execute
          arbitrary Python code on host machines — only install packs from trusted sources.
        </p>
      </section>

      <form onSubmit={handleSubmit} className="grid gap-3" noValidate>
        <label className="grid gap-1 text-sm" htmlFor="driver-tarball">
          <span className="font-medium text-text-2">Driver tarball</span>
          <input
            id="driver-tarball"
            ref={fileInputRef}
            type="file"
            accept={DRIVER_PACK_ACCEPT}
            onChange={handleFileChange}
            className="rounded border border-border bg-surface-2 px-2 py-1 text-sm text-text-1 file:mr-2 file:rounded file:border-0 file:bg-accent file:px-2 file:py-0.5 file:text-xs file:font-medium file:text-accent-on hover:file:bg-accent-hover"
          />
        </label>

        <label className="flex cursor-pointer items-start gap-2 text-sm">
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(e) => setConfirmed(e.target.checked)}
            aria-label="I confirm this driver may execute Python code on host machines."
            className="mt-0.5 h-4 w-4 rounded border-border accent-accent"
          />
          <span className="text-text-2">
            I confirm this driver may execute Python code on host machines.
          </span>
        </label>

        {errorMessage && (
          <p role="alert" className="rounded border border-danger-foreground bg-danger-soft px-3 py-2 text-sm text-danger-foreground">
            {errorMessage}
          </p>
        )}

        <div className="flex gap-2">
          <button
            type="submit"
            disabled={!canSubmit}
            className="rounded bg-accent px-3 py-1.5 text-sm font-medium text-accent-on hover:bg-accent-hover disabled:opacity-50"
          >
            {upload.isPending ? 'Uploading…' : 'Upload driver'}
          </button>
          {onClose && (
            <button
              type="button"
              onClick={onClose}
              className="rounded border border-border px-3 py-1.5 text-sm text-text-2 hover:bg-surface-2"
            >
              Cancel
            </button>
          )}
        </div>
      </form>
    </Card>
  );
}
