import { useState } from 'react';
import Modal from '../ui/Modal';
import { Button, Field, NumberField, Select, TextField } from '../ui';
import type { HostCreate, OSType } from '../../types';

type Props = {
  isOpen: boolean;
  isPending: boolean;
  onClose: () => void;
  onSubmit: (form: HostCreate) => Promise<void>;
};

const EMPTY_FORM: HostCreate = {
  hostname: '',
  ip: '',
  os_type: 'linux' as OSType,
  agent_port: 5100,
};

export default function AddHostModal({ isOpen, isPending, onClose, onSubmit }: Props) {
  const [form, setForm] = useState<HostCreate>(EMPTY_FORM);

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Add Host"
      footer={
        <>
          <Button variant="secondary" size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" form="add-host-form" size="sm" disabled={isPending}>
            {isPending ? 'Adding...' : 'Add Host'}
          </Button>
        </>
      }
    >
      <form
        id="add-host-form"
        onSubmit={async (event) => {
          event.preventDefault();
          await onSubmit(form);
          setForm(EMPTY_FORM);
        }}
        className="space-y-4"
      >
        <Field label="Hostname" htmlFor="add-host-hostname">
          <TextField
            id="add-host-hostname"
            required
            value={form.hostname}
            onChange={(value) => setForm({ ...form, hostname: value })}
          />
        </Field>
        <Field label="IP Address" htmlFor="add-host-ip">
          <TextField
            id="add-host-ip"
            required
            value={form.ip}
            onChange={(value) => setForm({ ...form, ip: value })}
          />
        </Field>
        <Field label="OS Type" htmlFor="add-host-os">
          <Select
            id="add-host-os"
            aria-label="OS Type"
            value={form.os_type}
            onChange={(value) => setForm({ ...form, os_type: value as OSType })}
            options={[
              { value: 'linux', label: 'Linux' },
              { value: 'macos', label: 'macOS' },
            ]}
            fullWidth
          />
        </Field>
        <Field label="Agent Port" htmlFor="add-host-agent-port">
          <NumberField
            id="add-host-agent-port"
            value={form.agent_port ?? null}
            onChange={(value) => setForm({ ...form, agent_port: value ?? 0 })}
          />
        </Field>
      </form>
    </Modal>
  );
}
