import Modal from '../ui/Modal';
import { UploadDriverPackForm } from './UploadDriverPackForm';

interface AddDriverDialogProps {
  isOpen: boolean;
  onClose: () => void;
}

export function AddDriverDialog({ isOpen, onClose }: AddDriverDialogProps) {
  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Upload Driver Pack" size="lg">
      <UploadDriverPackForm onSuccess={onClose} onClose={onClose} />
    </Modal>
  );
}
