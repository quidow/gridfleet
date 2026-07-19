import { Modal } from '../../components/ui/Modal';
import { Button } from '../../components/ui';

type DeviceActionErrorLine = {
  id: string;
  label: string;
  error: string;
};


type DeviceActionErrorsDialogProps = {
  isOpen: boolean;
  onClose: () => void;
  title: string;
  lines: DeviceActionErrorLine[];
};


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
