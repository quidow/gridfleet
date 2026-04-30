import { useState } from 'react';
import Modal from '../../components/ui/Modal';
import Button from '../../components/ui/Button';
import { Checkbox, DefinitionList, Field, Textarea, TextField } from '../../components/ui';
import { READINESS_GLOSSARY, deviceUpdateRequiresReverification } from '../../components/readiness';
import { useUpdateDevice } from '../../hooks/useDevices';
import DeviceManifestFields from './DeviceManifestFields';
import type { DevicePatch, DeviceRead, DeviceVerificationUpdate } from '../../types';
import { CONNECTION_TYPE_LABELS, DEVICE_AVAILABILITY_LABELS, resolvePlatformLabel } from '../../lib/labels';
import {
  buildUpdatePayload,
  getGeneratedDefaultsPreview,
  operatorTags,
  parseDeviceTagsInput,
  type VerificationRequest,
} from './devicePageHelpers';
import { useDriverPackCatalog } from '../../hooks/useDriverPacks';
import { findPlatformDescriptor } from '../../hooks/usePlatformDescriptor';

type Props = {
  device: DeviceRead | null;
  hostMap: Map<string, string>;
  onClose: () => void;
  onRequestVerification: (request: VerificationRequest) => void;
};

export default function DeviceEditModal({ device, hostMap, onClose, onRequestVerification }: Props) {
  if (!device) {
    return null;
  }

  return (
    <DeviceEditModalContent
      key={device.id}
      device={device}
      hostMap={hostMap}
      onClose={onClose}
      onRequestVerification={onRequestVerification}
    />
  );
}

function DeviceEditModalContent({ device, hostMap, onClose, onRequestVerification }: Omit<Props, 'device'> & { device: DeviceRead }) {
  const updateDevice = useUpdateDevice();
  const { data: catalog = [] } = useDriverPackCatalog();
  const platformId = device.platform_id;
  const descriptor = findPlatformDescriptor(catalog, device.pack_id, platformId);
  const [editForm, setEditForm] = useState<DevicePatch>(() => ({
    name: device.name,
    auto_manage: device.auto_manage,
    connection_target:
      device.connection_type === 'network' || device.connection_type === 'virtual'
        ? device.connection_target
        : undefined,
    ip_address: device.ip_address,
    device_config: (device.device_config ?? {}) as Record<string, unknown>,
  }));
  const [editTagsText, setEditTagsText] = useState(() => JSON.stringify(operatorTags(device.tags), null, 2));
  const [editTagsError, setEditTagsError] = useState<string | null>(null);
  const hostName = hostMap.get(device.host_id) ?? device.host_id;

  const generatedDefaults = getGeneratedDefaultsPreview({
    device_type: device.device_type,
  }, descriptor);

  return (
    <Modal
      isOpen={!!device}
      onClose={onClose}
      title="Edit Configuration"
      size="lg"
      footer={
        <>
          <Button variant="secondary" size="sm" onClick={onClose}>Cancel</Button>
          <Button type="submit" form="device-edit-form" size="sm" loading={updateDevice.isPending}>
            {updateDevice.isPending ? 'Saving...' : 'Save Changes'}
          </Button>
        </>
      }
    >
      <form
        id="device-edit-form"
        onSubmit={async (event) => {
          event.preventDefault();
          let parsedTags: Record<string, string>;
          try {
            parsedTags = parseDeviceTagsInput(editTagsText);
            setEditTagsError(null);
          } catch (error) {
            setEditTagsError(error instanceof Error ? error.message : 'Invalid JSON');
            return;
          }

          const nextBody = buildUpdatePayload(editForm, device, parsedTags);
          const readinessFieldIds = descriptor?.deviceFieldsSchema.map((field) => field.id) ?? [];
          if (deviceUpdateRequiresReverification(device, nextBody, readinessFieldIds)) {
            onRequestVerification({
              device,
              initialExistingForm: { host_id: device.host_id, ...nextBody } as DeviceVerificationUpdate,
              title: 'Save & Verify Device',
              handoffMessage:
                'These changes affect device readiness. The device will only be saved after guided re-verification succeeds.',
            });
            onClose();
            return;
          }

          await updateDevice.mutateAsync({ id: device.id, body: nextBody });
          onClose();
        }}
        className="space-y-4"
      >
        <div className="rounded-lg border border-border bg-surface-2 p-4 text-sm text-text-2">
          <p>{READINESS_GLOSSARY.identity}</p>
          <p className="mt-1">{READINESS_GLOSSARY.connectionTarget}</p>
          <p className="mt-1">
            Identity, platform, device type, host, connection type, OS version, and lifecycle status are now read-only in generic edit.
          </p>
          <p className="mt-1">
            Connection target edits stay available for existing network and virtual lanes, and readiness-impacting configuration changes still require guided re-verification.
          </p>
        </div>

        <div className="mb-4 rounded-md border border-border bg-surface-2 p-4">
          <DefinitionList
            layout="justified"
            items={[
              { term: 'Identity', definition: device.identity_value ?? '-' },
              { term: 'Platform', definition: resolvePlatformLabel(platformId, device.platform_label) },
              { term: 'Device Type', definition: device.device_type },
              { term: 'OS Version', definition: device.os_version ?? '-' },
              { term: 'Host', definition: hostName },
              { term: 'Connection Type', definition: CONNECTION_TYPE_LABELS[device.connection_type] },
              {
                term: 'Availability',
                definition: DEVICE_AVAILABILITY_LABELS[device.availability_status] ?? device.availability_status,
              },
            ]}
          />
        </div>

        <Field label="Name" htmlFor="edit-device-name">
          <TextField
            id="edit-device-name"
            value={editForm.name ?? ''}
            onChange={(value) => setEditForm({ ...editForm, name: value })}
          />
        </Field>

        <div>
          <Checkbox
            checked={editForm.auto_manage ?? device.auto_manage}
            onChange={(checked) => setEditForm({ ...editForm, auto_manage: checked })}
            label="Auto-manage"
          />
          <p className="mt-2 text-xs text-text-2">
            {(editForm.auto_manage ?? device.auto_manage)
              ? 'The manager can automatically recover this device when it becomes healthy again.'
              : 'Operators must return this device to service manually.'}
          </p>
        </div>

        {device.connection_type === 'network' || device.connection_type === 'virtual' ? (
          <Field label="Connection Target" htmlFor="edit-device-connection-target">
            <TextField
              id="edit-device-connection-target"
              value={editForm.connection_target ?? ''}
              onChange={(value) => setEditForm({ ...editForm, connection_target: value || null })}
            />
          </Field>
        ) : null}

        {device.connection_type === 'network' ? (
          <Field label="IP Address" htmlFor="edit-device-ip-address">
            <TextField
              id="edit-device-ip-address"
              value={editForm.ip_address ?? ''}
              onChange={(value) => setEditForm({ ...editForm, ip_address: value || null })}
            />
          </Field>
        ) : null}

        {descriptor && descriptor.deviceFieldsSchema.length > 0 && (
          <DeviceManifestFields
            fields={descriptor.deviceFieldsSchema}
            value={(editForm.device_config ?? {}) as Record<string, string | number | boolean>}
            onChange={(nextConfig) => setEditForm({ ...editForm, device_config: nextConfig })}
            idPrefix="edit-device-config"
          />
        )}

        <Field label="Tags JSON" htmlFor="edit-device-tags-json" error={editTagsError}>
          <Textarea
            id="edit-device-tags-json"
            value={editTagsText}
            onChange={(value) => {
              setEditTagsText(value);
              setEditTagsError(null);
            }}
            monospace
            invalid={!!editTagsError}
            rows={6}
          />
        </Field>

        {generatedDefaults.length > 0 ? (
          <div className="rounded-md border border-accent/25 bg-accent-soft p-3 text-sm text-text-1">
            <p className="mb-2 font-medium">Generated Defaults</p>
            <ul className="space-y-1">
              {generatedDefaults.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          </div>
        ) : null}

      </form>
    </Modal>
  );
}
