import { useState } from 'react';
import { Link } from 'react-router-dom';
import { FolderOpen, Pencil, Plus, Trash2 } from 'lucide-react';
import { ConfirmDialog } from '../components/ui/ConfirmDialog';
import { EmptyState } from '../components/ui/EmptyState';
import { FilterBuilder } from './devices/FilterBuilder';
import { LoadingSpinner } from '../components/LoadingSpinner';
import { Modal } from '../components/ui/Modal';
import { StatusBadge } from '../components/StatusBadge';
import { DataTable } from '../components/ui/DataTable';
import { Button } from '../components/ui/Button';
import { Field, Select, TextField } from '../components/ui';
import type { DataTableColumn } from '../components/ui/DataTable';
import { useDevices } from '../hooks/useDevices';
import { useDeviceGroups, useCreateDeviceGroup, useDeleteDeviceGroup } from '../hooks/useDeviceGroups';
import { useHosts } from '../hooks/useHosts';
import { createEmptyDeviceGroupFilterDraft, draftToDeviceGroupFilters } from '../lib/deviceGroupFilters';
import { usePageTitle } from '../hooks/usePageTitle';
import type { DeviceGroupCreate, DeviceGroupRead } from '../types';
import { PageHeader } from '../components/ui/PageHeader';
import { SectionErrorBoundary } from '../components/ErrorBoundary';

type GroupFormState = {
  key: string;
  name: string;
  description: string;
  group_type: 'static' | 'dynamic';
  filters: ReturnType<typeof createEmptyDeviceGroupFilterDraft>;
};

function createInitialFormState(): GroupFormState {
  return {
    key: '',
    name: '',
    description: '',
    group_type: 'static',
    filters: createEmptyDeviceGroupFilterDraft(),
  };
}

const COLUMNS: DataTableColumn<DeviceGroupRead>[] = [
  {
    key: 'name',
    header: 'Name',
    render: (group) => (
      <Link to={`/groups/${group.key}`} className="font-medium text-accent hover:text-accent-hover text-sm">
        {group.name}
      </Link>
    ),
  },
  {
    key: 'type',
    header: 'Type',
    render: (group) => <StatusBadge status={group.group_type} />,
  },
  {
    key: 'devices',
    header: 'Devices',
    render: (group) => <span className="text-sm text-text-2">{group.device_count}</span>,
  },
  {
    key: 'description',
    header: 'Description',
    render: (group) => <span className="text-sm text-text-3">{group.description || '-'}</span>,
  },
];

function suggestGroupKey(name: string): string {
  return name
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64)
    .replace(/-+$/g, '');
}

function DeviceGroupsContent() {
  const { data: groups, isLoading, dataUpdatedAt } = useDeviceGroups();
  const { data: allDevices = [] } = useDevices({});
  const { data: hosts = [] } = useHosts();
  const createGroup = useCreateDeviceGroup();
  const deleteGroup = useDeleteDeviceGroup();

  const [showAdd, setShowAdd] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<DeviceGroupRead | null>(null);
  const [form, setForm] = useState<GroupFormState>(createInitialFormState);
  const [isKeyManuallyEdited, setIsKeyManuallyEdited] = useState(false);

  function resetForm() {
    setForm(createInitialFormState());
    setIsKeyManuallyEdited(false);
  }

  const hostOptions = hosts.map((host) => ({ id: host.id, name: host.hostname }));
  const osVersionOptions = Array.from(new Set(allDevices.map((device) => device.os_version))).toSorted();

  if (isLoading) {
    return <LoadingSpinner />;
  }

  const columnsWithActions: DataTableColumn<DeviceGroupRead>[] = [
    ...COLUMNS,
    {
      key: 'actions',
      header: 'Actions',
      align: 'right',
      render: (group) => (
        <div className="flex items-center justify-end gap-1">
          <Link to={`/groups/${group.key}`} className="rounded p-1.5 text-text-3 hover:text-text-2" title="Edit">
            <Pencil size={15} />
          </Link>
          <button
            onClick={() => setDeleteTarget(group)}
            className="rounded p-1.5 text-text-3 hover:text-danger-foreground"
            title="Delete"
          >
            <Trash2 size={15} />
          </button>
        </div>
      ),
    },
  ];

  return (
    <>
      <PageHeader
        title="Device Groups"
        subtitle={`${groups?.length ?? 0} groups`}
        updatedAt={dataUpdatedAt}
        actions={
          <Button
            leadingIcon={<Plus size={16} />}
            onClick={() => { resetForm(); setShowAdd(true); }}
          >
            Create Group
          </Button>
        }
      />

      <div className="fade-in-stagger">
        <DataTable
          columns={columnsWithActions}
          rows={groups ?? []}
          rowKey={(g) => g.key}
          emptyState={
            <EmptyState
              icon={FolderOpen}
              title="No device groups"
              description="Create a group to manage devices together, or define a dynamic filter to keep a fleet segment in sync."
              action={
                <Button
                  leadingIcon={<Plus size={16} />}
                  onClick={() => { resetForm(); setShowAdd(true); }}
                >
                  Create Group
                </Button>
              }
            />
          }
        />
      </div>

      <Modal
        isOpen={showAdd}
        onClose={() => setShowAdd(false)}
        title="Create Device Group"
        footer={
          <>
            <Button variant="secondary" type="button" size="sm" onClick={() => setShowAdd(false)}>
              Cancel
            </Button>
            <Button type="submit" form="create-device-group-form" size="sm" loading={createGroup.isPending}>
              {createGroup.isPending ? 'Creating...' : 'Create Group'}
            </Button>
          </>
        }
      >
        <form
          id="create-device-group-form"
          onSubmit={async (event) => {
            event.preventDefault();
            const data: DeviceGroupCreate = {
              key: form.key,
              name: form.name,
              description: form.description || undefined,
              group_type: form.group_type,
              filters: form.group_type === 'dynamic' ? draftToDeviceGroupFilters(form.filters) : undefined,
            };
            await createGroup.mutateAsync(data);
            setShowAdd(false);
          }}
          className="space-y-4"
        >
          <Field label="Name" htmlFor="device-group-name">
            <TextField
              id="device-group-name"
              required
              value={form.name}
              onChange={(value) => {
                setForm(prev => {
                  if (!isKeyManuallyEdited) {
                    return { ...prev, name: value, key: suggestGroupKey(value) };
                  }
                  return { ...prev, name: value };
                });
              }}
            />
          </Field>
          <Field label="Group key" htmlFor="device-group-key">
            <TextField
              id="device-group-key"
              required
              value={form.key}
              onChange={(value) => {
                setIsKeyManuallyEdited(true);
                setForm({ ...form, key: value });
              }}
            />
          </Field>
          <Field label="Description" htmlFor="device-group-description">
            <TextField
              id="device-group-description"
              value={form.description}
              onChange={(value) => setForm({ ...form, description: value })}
            />
          </Field>
          <Field label="Type" htmlFor="device-group-type">
            <Select
              id="device-group-type"
              value={form.group_type}
              onChange={(value) => setForm({ ...form, group_type: value as 'static' | 'dynamic' })}
              options={[
                { value: 'static', label: 'Static (manual members)' },
                { value: 'dynamic', label: 'Dynamic (filter-driven)' },
              ]}
              fullWidth
            />
          </Field>
          {form.group_type === 'dynamic' ? (
            <FilterBuilder
              filters={form.filters}
              onChange={(filters) => setForm({ ...form, filters })}
              hostOptions={hostOptions}
              osVersionOptions={osVersionOptions}
            />
          ) : null}
        </form>
      </Modal>

      <ConfirmDialog
        isOpen={!!deleteTarget}
        onClose={() => setDeleteTarget(null)}
        onConfirm={async () => {
          if (deleteTarget) await deleteGroup.mutateAsync(deleteTarget.key);
          setDeleteTarget(null);
        }}
        title="Delete Group"
        message={`Are you sure you want to delete "${deleteTarget?.name}"?`}
        confirmLabel="Delete"
        variant="danger"
      />
    </>
  );
}

export function DeviceGroups() {
  usePageTitle('Device Groups');

  return (
    <div>
      <SectionErrorBoundary scope="device-groups">
        <DeviceGroupsContent />
      </SectionErrorBoundary>
    </div>
  );
}
