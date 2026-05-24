import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Modal } from '../ui/Modal';
import { Tabs } from '../ui/Tabs';
import { UploadDriverPackForm } from './UploadDriverPackForm';
import { TemplatePackForm } from './TemplatePackForm';
import { ForkPackForm } from './ForkPackForm';

interface AddDriverDialogProps {
  isOpen: boolean;
  onClose: () => void;
}

const TABS = [
  { id: 'template', label: 'From Template' },
  { id: 'upload', label: 'Upload Tarball' },
  { id: 'fork', label: 'Fork Existing' },
];

export function AddDriverDialog({ isOpen, onClose }: AddDriverDialogProps) {
  const [tab, setTab] = useState('template');
  const navigate = useNavigate();

  function handleCreated(packId: string) {
    onClose();
    navigate(`/drivers/${encodeURIComponent(packId)}`);
  }

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Add Driver Pack" size="lg">
      <Tabs tabs={TABS} activeId={tab} onChange={setTab} className="mb-4" />

      {tab === 'template' && <TemplatePackForm onSuccess={handleCreated} onClose={onClose} />}
      {tab === 'upload' && <UploadDriverPackForm onSuccess={onClose} onClose={onClose} />}
      {tab === 'fork' && <ForkPackForm onSuccess={handleCreated} onClose={onClose} />}
    </Modal>
  );
}
