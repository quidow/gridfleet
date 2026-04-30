import { type FormEvent, useState } from 'react';
import Modal from '../../components/ui/Modal';
import Button from '../../components/ui/Button';
import { Field, Select, TextField } from '../../components/ui';
import DeviceVerificationProgress from './DeviceVerificationProgress';
import DeviceManifestFields from './DeviceManifestFields';
import type {
  ConnectionType,
  DeviceRead,
  DeviceType,
  DeviceVerificationUpdate,
  DeviceVerificationJob,
} from '../../types';
import { useStartExistingDeviceVerification } from '../../hooks/useDevices';
import { buildDeviceFieldLabelMap, missingSetupFieldLabel, READINESS_GLOSSARY } from '../../components/readiness';
import {
  buildExistingVerificationForm,
  buildExistingVerificationPayload,
  CONNECTION_TYPE_LABELS,
  DEVICE_TYPE_LABELS,
  generatedConfigPreview,
  getAllowedConnectionTypes,
  getAllowedDeviceTypes,
  normalizeFormForDescriptor,
  showConnectionTypeField,
  showDeviceTypeField,
  showIpAddressField,
  showOsVersionField,
  useDeviceVerificationJobController,
} from './deviceVerificationWorkflow';
import { resolvePlatformLabel } from '../../lib/labels';
import { useDriverPackCatalog } from '../../hooks/useDriverPacks';
import { findPlatformDescriptor, makePlatformKey, parsePlatformKey } from '../../hooks/usePlatformDescriptor';

type Props = {
  isOpen: boolean;
  onClose: () => void;
  onCompleted?: () => void;
  existingDevice: DeviceRead;
  initialExistingForm?: DeviceVerificationUpdate;
  handoffMessage?: string;
  title?: string;
};

export default function SetupVerificationModal({
  isOpen,
  onClose,
  onCompleted,
  existingDevice,
  initialExistingForm,
  handoffMessage,
  title,
}: Props) {
  const startExistingVerification = useStartExistingDeviceVerification();
  const { data: catalog = [] } = useDriverPackCatalog();
  const initialDescriptor = findPlatformDescriptor(catalog, existingDevice.pack_id, existingDevice.platform_id);
  const [existingForm, setExistingForm] = useState<DeviceVerificationUpdate>(() =>
    ({ ...buildExistingVerificationForm(existingDevice, initialDescriptor), ...(initialExistingForm ?? {}) })
  );
  const [job, setJob] = useState<DeviceVerificationJob | null>(null);
  const activePackId = existingForm.pack_id ?? existingDevice.pack_id;
  const activePlatformId = existingForm.platform_id ?? existingDevice.platform_id ?? '';
  const activeDescriptor = findPlatformDescriptor(catalog, activePackId, activePlatformId);
  const activeForm = {
    ...buildExistingVerificationForm(existingDevice, activeDescriptor),
    ...existingForm,
  } as DeviceVerificationUpdate;
  const activePlatformKey =
    activePackId && activePlatformId ? makePlatformKey(activePackId, activePlatformId) : '';
  const platformOptions = catalog.flatMap((pack) =>
    (pack.platforms ?? []).map((p) => ({
      value: makePlatformKey(pack.id, p.id),
      label: `${pack.display_name}: ${resolvePlatformLabel(p.id, p.display_name)}`,
    })),
  );
  const extraInvalidationKeys = [['device', existingDevice.id], ['device-config', existingDevice.id], ['config-history', existingDevice.id]] as const;
  const { activeJob, isVerificationRunning, resetCompletionGuard } = useDeviceVerificationJobController({
    isOpen,
    isStarting: startExistingVerification.isPending,
    job,
    onJobChange: setJob,
    onCompleted,
    onClose,
    extraInvalidationKeys,
  });
  const configPreview = generatedConfigPreview(activeForm, activeDescriptor);
  const fieldPrefix = `device-verification-${existingDevice.id}`;
  const setupFieldLabels = buildDeviceFieldLabelMap(activeDescriptor?.deviceFieldsSchema ?? []);

  function closeModal() {
    if (isVerificationRunning) return;
    onClose();
  }

  const visibleMissingFields = (existingDevice?.missing_setup_fields ?? []).map((field) =>
    missingSetupFieldLabel(field, setupFieldLabels),
  );

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    resetCompletionGuard();
    const nextJob = await startExistingVerification.mutateAsync({
      id: existingDevice.id,
      body: buildExistingVerificationPayload(existingForm, existingDevice, activeDescriptor),
    });
    setJob(nextJob);
  }

  return (
    <Modal
      isOpen={isOpen}
      onClose={closeModal}
      title={title ?? 'Complete Setup'}
      size="lg"
      footer={
        <>
          <Button variant="secondary" size="sm" onClick={closeModal} disabled={isVerificationRunning}>Cancel</Button>
          <Button
            type="submit"
            form="setup-verification-form"
            size="sm"
            disabled={isVerificationRunning}
          >
            {isVerificationRunning
              ? 'Verifying...'
              : activeJob?.status === 'failed'
                ? 'Retry Verification'
                : existingDevice.readiness_state === 'setup_required'
                  ? 'Complete Setup & Verify'
                  : 'Verify Device'}
          </Button>
        </>
      }
    >
      <form id="setup-verification-form" onSubmit={handleSubmit} className="space-y-4">
        {(existingDevice.readiness_state !== 'verified' || handoffMessage) && (
          <div className="rounded-lg border border-accent/30 bg-accent-soft p-4 text-sm text-text-1">
            <p className="font-medium">
              {handoffMessage
                ?? (existingDevice.readiness_state === 'setup_required'
                  ? 'Complete the missing setup fields, then verify.'
                  : 'Verify this device before it can be used.')}
            </p>
            {visibleMissingFields.length > 0 && (
              <p className="mt-1">Missing: {visibleMissingFields.join(', ')}</p>
            )}
          </div>
        )}

        <div className="rounded-lg border border-border bg-surface-2 p-4 text-sm text-text-2">
          <p>{READINESS_GLOSSARY.identity}</p>
          <p className="mt-1">{READINESS_GLOSSARY.connectionTarget}</p>
          <p className="mt-1">{READINESS_GLOSSARY.setupRequired}</p>
          <p className="mt-1">{READINESS_GLOSSARY.verificationRequired}</p>
        </div>

        <DeviceVerificationProgress
          activeJob={activeJob}
          showStartError={startExistingVerification.isError}
        />

        <fieldset disabled={isVerificationRunning} className="space-y-4 disabled:opacity-70">
          <Field label="Name" htmlFor={`${fieldPrefix}-name`}>
            <TextField
              id={`${fieldPrefix}-name`}
              required
              value={activeForm.name ?? ''}
              onChange={(value) => setExistingForm({ ...existingForm, name: value })}
            />
          </Field>
          <Field label="Platform" htmlFor={`${fieldPrefix}-platform`}>
            <Select
              id={`${fieldPrefix}-platform`}
              value={activePlatformKey}
              onChange={(value) => {
                const parsed = parsePlatformKey(value);
                if (!parsed) return;
                setExistingForm(normalizeFormForDescriptor(
                  { ...existingForm, pack_id: parsed.packId, platform_id: parsed.platformId },
                  findPlatformDescriptor(catalog, parsed.packId, parsed.platformId),
                ));
              }}
              options={platformOptions}
              fullWidth
            />
          </Field>
          <Field label="Identity" htmlFor={`${fieldPrefix}-identity`}>
            <TextField
              id={`${fieldPrefix}-identity`}
              value={activeForm.identity_value ?? ''}
              onChange={(value) => setExistingForm({ ...existingForm, identity_value: value || null })}
            />
          </Field>
          <Field label="Connection Target" htmlFor={`${fieldPrefix}-connection-target`}>
            <TextField
              id={`${fieldPrefix}-connection-target`}
              required={!!activeDescriptor}
              value={activeForm.connection_target ?? ''}
              onChange={(value) => setExistingForm({ ...existingForm, connection_target: value || null })}
            />
          </Field>
          {showDeviceTypeField(activeDescriptor) && (
            <Field label="Device Type" htmlFor={`${fieldPrefix}-device-type`}>
              <Select
                id={`${fieldPrefix}-device-type`}
                value={activeForm.device_type ?? ''}
                onChange={(value) => {
                  const deviceType = (value as DeviceType) || null;
                  setExistingForm(normalizeFormForDescriptor(
                    { ...existingForm, device_type: deviceType },
                    activeDescriptor,
                  ));
                }}
                options={getAllowedDeviceTypes(activeDescriptor).map((deviceType) => ({
                  value: deviceType,
                  label: DEVICE_TYPE_LABELS[deviceType],
                }))}
                fullWidth
              />
            </Field>
          )}
          {showConnectionTypeField(activeDescriptor, activeForm.device_type as DeviceType | null | undefined) && (
            <Field label="Connection Type" htmlFor={`${fieldPrefix}-connection-type`}>
              <Select
                id={`${fieldPrefix}-connection-type`}
                value={activeForm.connection_type ?? ''}
                onChange={(value) => {
                  const connectionType = (value as ConnectionType) || null;
                  setExistingForm(normalizeFormForDescriptor(
                    { ...existingForm, connection_type: connectionType },
                    activeDescriptor,
                  ));
                }}
                options={getAllowedConnectionTypes(
                  activeDescriptor,
                  activeForm.device_type as DeviceType | null | undefined,
                ).map((connectionType) => ({
                  value: connectionType,
                  label: CONNECTION_TYPE_LABELS[connectionType],
                }))}
                fullWidth
              />
            </Field>
          )}
          {showIpAddressField(activeForm) && (
            <Field label="IP Address" htmlFor={`${fieldPrefix}-ip-address`}>
            <TextField
              id={`${fieldPrefix}-ip-address`}
              value={activeForm.ip_address ?? ''}
              onChange={(value) => setExistingForm({ ...existingForm, ip_address: value || null })}
              placeholder="e.g. 192.168.1.42"
            />
            </Field>
          )}
          {showOsVersionField(activeDescriptor) && (
            <Field label="OS Version" htmlFor={`${fieldPrefix}-os-version`}>
            <TextField
              id={`${fieldPrefix}-os-version`}
              value={activeForm.os_version ?? ''}
              onChange={(value) => setExistingForm({ ...existingForm, os_version: value })}
            />
          </Field>
          )}
          {activeDescriptor && activeDescriptor.deviceFieldsSchema.length > 0 && (
            <DeviceManifestFields
              fields={activeDescriptor.deviceFieldsSchema}
              value={(activeForm.device_config ?? {}) as Record<string, string | number | boolean>}
              onChange={(nextConfig) => setExistingForm({ ...existingForm, device_config: nextConfig })}
              idPrefix={`${fieldPrefix}-config`}
            />
          )}
          {configPreview.length > 0 && (
            <div className="rounded-md border border-accent/30 bg-accent-soft p-3 text-sm text-text-1">
              <p className="mb-2 font-medium">Generated Defaults</p>
              <ul className="space-y-1">
                {configPreview.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            </div>
          )}
        </fieldset>
      </form>
    </Modal>
  );
}
