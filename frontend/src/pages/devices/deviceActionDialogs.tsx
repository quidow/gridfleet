import Modal from '../../components/ui/Modal';
import { Button, Checkbox, Field, Textarea } from '../../components/ui';

type DeviceActionErrorLine = {
  id: string;
  label: string;
  error: string;
};

type TagsActionDialogProps = {
  isOpen: boolean;
  onClose: () => void;
  title: string;
  tagsText: string;
  merge: boolean;
  mergeLabel: string;
  tagsError: string | null;
  onTagsTextChange: (value: string) => void;
  onMergeChange: (value: boolean) => void;
  onConfirm: () => void | Promise<void>;
  confirmLabel?: string;
};

type DeviceActionErrorsDialogProps = {
  isOpen: boolean;
  onClose: () => void;
  title: string;
  lines: DeviceActionErrorLine[];
};

export function TagsActionDialog({
  isOpen,
  onClose,
  title,
  tagsText,
  merge,
  mergeLabel,
  tagsError,
  onTagsTextChange,
  onMergeChange,
  onConfirm,
  confirmLabel = 'Save Tags',
}: TagsActionDialogProps) {
  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title={title}
      footer={
        <>
          <Button variant="secondary" size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" size="sm" onClick={() => onConfirm()}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label="Tags JSON" htmlFor="tags-json" error={tagsError ?? undefined}>
          <Textarea
            id="tags-json"
            aria-label="Tags JSON"
            value={tagsText}
            onChange={onTagsTextChange}
            monospace
            invalid={!!tagsError}
            rows={8}
          />
        </Field>
        <Checkbox checked={merge} onChange={onMergeChange} label={mergeLabel} />
      </div>
    </Modal>
  );
}

export function DeviceActionErrorsDialog({
  isOpen,
  onClose,
  title,
  lines,
}: DeviceActionErrorsDialogProps) {
  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title={title}
      footer={
        <Button variant="secondary" size="sm" onClick={onClose}>
          Close
        </Button>
      }
    >
      <div className="space-y-3">
        {lines.length === 0 ? (
          <p className="text-sm text-text-3">No per-device errors captured.</p>
        ) : (
          lines.map((line) => (
            <div key={line.id} className="rounded-md border border-warning-strong/30 bg-warning-soft px-3 py-2">
              <p className="text-sm font-medium text-warning-foreground">{line.label}</p>
              <p className="text-sm text-warning-foreground">{line.error}</p>
            </div>
          ))
        )}
      </div>
    </Modal>
  );
}
