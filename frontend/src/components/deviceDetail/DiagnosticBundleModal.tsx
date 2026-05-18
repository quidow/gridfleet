import { useMemo } from 'react';

import { Button, Modal } from '../ui';

type Props = {
  open: boolean;
  title: string;
  payload: unknown;
  redacted: boolean;
  loading?: boolean;
  toggling?: boolean;
  error?: string | null;
  onClose: () => void;
  onToggleRedact: () => void;
};

export default function DiagnosticBundleModal({
  open,
  title,
  payload,
  redacted,
  loading,
  toggling,
  error,
  onClose,
  onToggleRedact,
}: Props) {
  const text = useMemo(() => JSON.stringify(payload, null, 2), [payload]);

  const handleCopy = () => {
    void navigator.clipboard.writeText(text);
  };

  const handleDownload = () => {
    const blob = new Blob([text], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `${title.replace(/\s+/g, '_')}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  };

  return (
    <Modal
      isOpen={open}
      onClose={onClose}
      title={title}
      size="xl"
      footer={
        <>
          <Button onClick={onToggleRedact} variant="secondary" size="sm" loading={toggling}>
            {redacted ? 'Show unredacted' : 'Redact'}
          </Button>
          <Button onClick={handleCopy} variant="secondary" size="sm">
            Copy
          </Button>
          <Button onClick={handleDownload} variant="secondary" size="sm">
            Download
          </Button>
          <Button onClick={onClose} size="sm">
            Close
          </Button>
        </>
      }
    >
      <div className="max-h-[70vh] overflow-auto rounded-md bg-surface-2 px-4 py-3">
        {loading ? (
          <p className="text-sm text-text-2">Loading...</p>
        ) : error ? (
          <p className="text-sm text-danger">{error}</p>
        ) : (
          <pre className="whitespace-pre-wrap break-words text-xs leading-relaxed text-text-1">{text}</pre>
        )}
      </div>
    </Modal>
  );
}
