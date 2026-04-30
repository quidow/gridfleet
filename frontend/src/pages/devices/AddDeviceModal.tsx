import { type FormEvent, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import Modal from '../../components/ui/Modal';
import Button from '../../components/ui/Button';
import { Field, Select, TextField } from '../../components/ui';
import DeviceVerificationProgress from './DeviceVerificationProgress';
import DeviceManifestFields, { defaultsForDeviceFields, type DeviceConfigDraft } from './DeviceManifestFields';
import type {
  ConnectionType,
  DeviceType,
  DeviceVerificationCreate,
  DeviceVerificationJob,
} from '../../types';
import { useStartDeviceVerification } from '../../hooks/useDevices';
import { useIntakeCandidates } from '../../hooks/useHosts';
import {
  CONNECTION_TYPE_LABELS,
  DEVICE_TYPE_LABELS,
  type HostOption,
  filterIntakeCandidates,
  getAllowedConnectionTypes,
  getAllowedDeviceTypes,
  laneNeedsCandidate,
  manualRegistrationRequirements,
  parseIpAddress,
  showConnectionTypeField,
  showDeviceTypeField,
  useDeviceVerificationJobController,
} from './deviceVerificationWorkflow';
import { useDriverPackCatalog } from '../../hooks/useDriverPacks';
import { resolvePlatformLabel } from '../../lib/labels';
import {
  findPlatformDescriptorByKey,
  makePlatformKey,
  platformDescriptorForDeviceType,
} from '../../hooks/usePlatformDescriptor';

type Props = {
  isOpen: boolean;
  onClose: () => void;
  onCompleted?: () => void;
  hostOptions: HostOption[];
};

export default function AddDeviceModal({ isOpen, onClose, onCompleted, hostOptions }: Props) {
  const startVerification = useStartDeviceVerification();
  const [hostId, setHostId] = useState<string>('');
  const { data: catalog = [] } = useDriverPackCatalog();

  const platformOptions = useMemo(() => {
    const drafts: Array<{
      value: string;
      baseLabel: string;
      packLabel: string;
      packId: string;
      platformId: string;
      deviceTypes: string[];
      connectionTypes: string[];
    }> = [];
    const baseLabelCounts = new Map<string, number>();
    for (const pack of catalog) {
      if (pack.state !== 'enabled') continue;
      for (const platform of pack.platforms ?? []) {
        const baseLabel = resolvePlatformLabel(platform.id, platform.display_name);
        baseLabelCounts.set(baseLabel, (baseLabelCounts.get(baseLabel) ?? 0) + 1);
        drafts.push({
          value: makePlatformKey(pack.id, platform.id),
          baseLabel,
          packLabel: pack.display_name,
          packId: pack.id,
          platformId: platform.id,
          deviceTypes: platform.device_types,
          connectionTypes: platform.connection_types,
        });
      }
    }
    const labels = drafts.map((draft) => platformOptionLabel(draft, (baseLabelCounts.get(draft.baseLabel) ?? 0) > 1));
    const labelCounts = new Map<string, number>();
    for (const label of labels) {
      labelCounts.set(label, (labelCounts.get(label) ?? 0) + 1);
    }
    return drafts.map((draft, index) => {
      const label = labels[index];
      return {
        value: draft.value,
        label: (labelCounts.get(label) ?? 0) > 1 ? `${label} - ${draft.packLabel}` : label,
        packId: draft.packId,
        platformId: draft.platformId,
      };
    });
  }, [catalog]);

  const defaultPlatformKey = platformOptions[0]?.value ?? '';

  const [platformKey, setPlatformKey] = useState<string>(defaultPlatformKey);
  const activePlatformKey = platformOptions.some((opt) => opt.value === platformKey)
    ? platformKey
    : defaultPlatformKey;

  const [deviceType, setDeviceType] = useState<DeviceType>('real_device');
  const [connectionType, setConnectionType] = useState<ConnectionType>('usb');
  const [selectedCandidateKey, setSelectedCandidateKey] = useState<string>('');
  const [displayName, setDisplayName] = useState('');
  const [manualConnectionTarget, setManualConnectionTarget] = useState('');
  const [manualIpAddress, setManualIpAddress] = useState('');
  const [deviceConfig, setDeviceConfig] = useState<DeviceConfigDraft>({});
  const [job, setJob] = useState<DeviceVerificationJob | null>(null);
  const { data: candidates = [], isFetching: isFetchingCandidates = false } = useIntakeCandidates(hostId || null);
  const [candidateUpdate, setCandidateUpdate] = useState<{ context: string; signature: string } | null>(null);
  const previousCandidateSignatureRef = useRef<string | null>(null);
  const previousCandidateContextRef = useRef<string | null>(null);
  const { activeJob, isVerificationRunning, resetCompletionGuard } = useDeviceVerificationJobController({
    isOpen,
    isStarting: startVerification.isPending,
    job,
    onJobChange: setJob,
    onCompleted,
    onClose,
  });

  const activeDescriptor = findPlatformDescriptorByKey(catalog, activePlatformKey);
  const activeAllowedDeviceTypes = getAllowedDeviceTypes(activeDescriptor);
  const activeDeviceType = activeAllowedDeviceTypes.includes(deviceType)
    ? deviceType
    : activeAllowedDeviceTypes[0] ?? deviceType;
  const activeAllowedConnectionTypes = getAllowedConnectionTypes(activeDescriptor, activeDeviceType);
  const activeConnectionType = activeAllowedConnectionTypes.includes(connectionType)
    ? connectionType
    : activeAllowedConnectionTypes[0] ?? connectionType;
  const effectiveDescriptor = platformDescriptorForDeviceType(activeDescriptor, activeDeviceType);

  const filteredCandidates = useMemo(
    () => filterIntakeCandidates(candidates, activeDescriptor, activeDeviceType, activeConnectionType),
    [candidates, activeDescriptor, activeDeviceType, activeConnectionType],
  );
  const candidateSignature = useMemo(
    () => filteredCandidates
      .map((candidate) => `${candidate.identity_value}:${candidate.connection_target ?? ''}:${candidate.already_registered}`)
      .sort()
      .join('|'),
    [filteredCandidates],
  );
  const candidateContext = `${hostId}:${activePlatformKey}:${activeDeviceType}:${activeConnectionType}`;
  const selectedCandidate = filteredCandidates.find(
    (candidate) => `${candidate.identity_value}:${candidate.connection_target ?? ''}` === selectedCandidateKey,
  );
  const observedDeviceCountLabel = `${filteredCandidates.length} ${filteredCandidates.length === 1 ? 'device' : 'devices'} observed`;
  const showCandidateUpdate =
    candidateUpdate?.context === candidateContext && candidateUpdate.signature === candidateSignature;

  const manualRequirements = manualRegistrationRequirements(activeDescriptor, activeDeviceType, activeConnectionType);
  const requiresIpAddress = manualRequirements.ipAddress;
  const requiresConnectionTarget = manualRequirements.connectionTarget;
  const candidateRequired = laneNeedsCandidate(activeDescriptor, activeDeviceType, activeConnectionType);
  const showIpField = requiresIpAddress && !selectedCandidate;
  const showConnectionTargetField = requiresConnectionTarget && !selectedCandidate;
  const manualEntryComplete =
    (!requiresConnectionTarget || !!manualConnectionTarget.trim()) &&
    (!requiresIpAddress || !!(manualIpAddress.trim() || parseIpAddress(manualConnectionTarget)));
  const descriptorConfigKey = effectiveDescriptor
    ? `${effectiveDescriptor.packId}:${effectiveDescriptor.platformId}:${activeDeviceType}`
    : '';
  const previousDescriptorConfigKeyRef = useRef<string>('');

  useEffect(() => {
    if (previousDescriptorConfigKeyRef.current === descriptorConfigKey) return;
    previousDescriptorConfigKeyRef.current = descriptorConfigKey;
    setDeviceConfig(effectiveDescriptor ? defaultsForDeviceFields(effectiveDescriptor.deviceFieldsSchema) : {});
  }, [descriptorConfigKey, effectiveDescriptor]);

  function closeModal() {
    if (isVerificationRunning) return;
    onClose();
  }

  function selectHost(nextHostId: string) {
    setHostId(nextHostId);
    setSelectedCandidateKey('');
  }

  function selectPlatform(nextKey: string) {
    const nextDescriptor = findPlatformDescriptorByKey(catalog, nextKey);
    const allowedDeviceTypes = getAllowedDeviceTypes(nextDescriptor);
    const nextDeviceType = allowedDeviceTypes.includes(activeDeviceType)
      ? activeDeviceType
      : allowedDeviceTypes[0];
    const allowedConnectionTypes = getAllowedConnectionTypes(nextDescriptor, nextDeviceType);
    const nextConnectionType = allowedConnectionTypes.includes(activeConnectionType)
      ? activeConnectionType
      : allowedConnectionTypes[0];
    setPlatformKey(nextKey);
    setDeviceType(nextDeviceType);
    setConnectionType(nextConnectionType);
    setSelectedCandidateKey('');
    setManualIpAddress('');
    setManualConnectionTarget('');
    const nextEffectiveDescriptor = platformDescriptorForDeviceType(nextDescriptor, nextDeviceType);
    setDeviceConfig(nextEffectiveDescriptor ? defaultsForDeviceFields(nextEffectiveDescriptor.deviceFieldsSchema) : {});
  }

  function selectDeviceType(nextDeviceType: DeviceType) {
    const allowedConnectionTypes = getAllowedConnectionTypes(activeDescriptor, nextDeviceType);
    const nextConnectionType = allowedConnectionTypes.includes(activeConnectionType)
      ? activeConnectionType
      : allowedConnectionTypes[0];
    setDeviceType(nextDeviceType);
    setConnectionType(nextConnectionType);
    setSelectedCandidateKey('');
    const nextEffectiveDescriptor = platformDescriptorForDeviceType(activeDescriptor, nextDeviceType);
    setDeviceConfig(nextEffectiveDescriptor ? defaultsForDeviceFields(nextEffectiveDescriptor.deviceFieldsSchema) : {});
  }

  function selectConnectionType(nextConnectionType: ConnectionType) {
    setConnectionType(nextConnectionType);
    setSelectedCandidateKey('');
  }

  useEffect(() => {
    if (!hostId) {
      previousCandidateContextRef.current = null;
      previousCandidateSignatureRef.current = null;
      return;
    }

    if (previousCandidateContextRef.current !== candidateContext) {
      previousCandidateContextRef.current = candidateContext;
      previousCandidateSignatureRef.current = candidateSignature;
      return;
    }

    if (
      previousCandidateSignatureRef.current !== null &&
      previousCandidateSignatureRef.current !== candidateSignature
    ) {
      previousCandidateSignatureRef.current = candidateSignature;
      const showTimeout = window.setTimeout(() => {
        setCandidateUpdate({ context: candidateContext, signature: candidateSignature });
      }, 0);
      const hideTimeout = window.setTimeout(() => setCandidateUpdate(null), 3500);
      return () => {
        window.clearTimeout(showTimeout);
        window.clearTimeout(hideTimeout);
      };
    }

    previousCandidateSignatureRef.current = candidateSignature;
  }, [candidateContext, candidateSignature, hostId]);

  const displayNameValue = displayName || selectedCandidate?.name || '';
  const derivedConnectionTarget =
    selectedCandidate?.connection_target ?? manualConnectionTarget.trim();
  const derivedIdentity =
    selectedCandidate?.identity_value ?? 'Resolved during verification';
  const derivedOsVersion =
    selectedCandidate?.os_version ?? 'Resolved during verification';
  const candidateSummary = selectedCandidate
    ? `${selectedCandidate.name} • ${selectedCandidate.model || resolvePlatformLabel(selectedCandidate.platform_id, selectedCandidate.platform_label)} • ${selectedCandidate.os_version}`
    : null;

  const canSubmit =
    !!hostId &&
    !!activeDescriptor &&
    (
      (candidateRequired && !!selectedCandidate && !selectedCandidate.already_registered) ||
      (!!selectedCandidate && !selectedCandidate.already_registered) ||
      (!candidateRequired && manualEntryComplete)
    );

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit || !activeDescriptor) return;
    resetCompletionGuard();

    const name = displayNameValue.trim() || derivedConnectionTarget || manualIpAddress.trim();
    const body: DeviceVerificationCreate = {
      host_id: hostId,
      name,
      pack_id: selectedCandidate?.pack_id ?? activeDescriptor.packId,
      platform_id: selectedCandidate?.platform_id ?? activeDescriptor.platformId,
      identity_scheme: selectedCandidate?.identity_scheme ?? (effectiveDescriptor ?? activeDescriptor).identityScheme,
      identity_scope: selectedCandidate?.identity_scope ?? (effectiveDescriptor ?? activeDescriptor).identityScope,
      device_type: selectedCandidate?.device_type ?? activeDeviceType,
      connection_type: selectedCandidate?.connection_type ?? activeConnectionType,
      identity_value: selectedCandidate?.identity_value ?? null,
      connection_target: selectedCandidate?.connection_target ?? (requiresConnectionTarget ? manualConnectionTarget.trim() || null : null),
      os_version: selectedCandidate?.os_version ?? 'unknown',
      manufacturer: selectedCandidate?.manufacturer || null,
      model: selectedCandidate?.model || null,
      model_number: selectedCandidate?.model_number || null,
      software_versions: selectedCandidate?.software_versions ?? null,
      ip_address: selectedCandidate?.ip_address ?? (manualIpAddress.trim() || parseIpAddress(manualConnectionTarget)),
      device_config: Object.keys(deviceConfig).length > 0 ? deviceConfig : null,
    };
    const nextJob = await startVerification.mutateAsync(body);
    setJob(nextJob);
  }

  return (
    <Modal
      isOpen={isOpen}
      onClose={closeModal}
      title="Add Device"
      size="lg"
      footer={
        <>
          <Button variant="secondary" size="sm" onClick={closeModal} disabled={isVerificationRunning}>Cancel</Button>
          <Button
            type="submit"
            form="add-device-form"
            size="sm"
            disabled={!canSubmit || isVerificationRunning}
          >
            {isVerificationRunning ? 'Verifying...' : activeJob?.status === 'failed' ? 'Retry Verification' : 'Verify & Add Device'}
          </Button>
        </>
      }
    >
      <form id="add-device-form" onSubmit={handleSubmit} className="space-y-4">
        <div className="rounded-lg border border-accent/30 bg-accent-soft p-4 text-sm text-text-1">
          <p className="font-medium">Pick a host, then choose a driver platform for this device.</p>
          <p className="mt-1">
            Discovery can prefill identity and connection details. Manual entry is available when the selected driver provides enough registration fields.
          </p>
        </div>

        <DeviceVerificationProgress activeJob={activeJob} showStartError={startVerification.isError} />

        <fieldset disabled={isVerificationRunning} className="space-y-4 disabled:opacity-70">
          <Field label="Host" htmlFor="add-device-host">
            <Select
              id="add-device-host"
              required
              value={hostId}
              onChange={selectHost}
              placeholder="Select a host"
              options={hostOptions.map((host) => ({ value: host.id, label: host.name }))}
              fullWidth
            />
          </Field>

          {hostId && (
            <>
              {platformOptions.length === 0 ? (
                <div className="rounded-lg border border-warning-strong bg-warning-soft px-4 py-3 text-sm text-warning-foreground">
                  No driver packs are enabled. Upload a driver pack from the{' '}
                  <Link to="/drivers" className="font-medium underline" onClick={closeModal}>
                    Drivers
                  </Link>{' '}
                  page before registering devices.
                </div>
              ) : (
                <>
                  <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
                    <Field label="Platform" htmlFor="add-device-platform">
                      <Select
                        id="add-device-platform"
                        value={activePlatformKey}
                        onChange={selectPlatform}
                        options={platformOptions.map(({ value, label }) => ({ value, label }))}
                        fullWidth
                      />
                    </Field>
                    {showDeviceTypeField(activeDescriptor) && (
                      <Field label="Device Type" htmlFor="add-device-type">
                        <Select
                          id="add-device-type"
                          value={activeDeviceType}
                          onChange={(value) => selectDeviceType(value as DeviceType)}
                          options={getAllowedDeviceTypes(activeDescriptor).map((value) => ({
                            value,
                            label: DEVICE_TYPE_LABELS[value],
                          }))}
                          fullWidth
                        />
                      </Field>
                    )}
                    {showConnectionTypeField(activeDescriptor, activeDeviceType) && (
                      <Field label="Connection Type" htmlFor="add-device-connection">
                        <Select
                          id="add-device-connection"
                          value={activeConnectionType}
                          onChange={(value) => selectConnectionType(value as ConnectionType)}
                          options={getAllowedConnectionTypes(activeDescriptor, activeDeviceType).map((value) => ({
                            value,
                            label: CONNECTION_TYPE_LABELS[value],
                          }))}
                          fullWidth
                        />
                      </Field>
                    )}
                  </div>

                  <div className="rounded-md border border-border bg-surface-2 p-3 text-sm">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div>
                        <p className="font-medium text-text-1">Observed Devices</p>
                        <p className="mt-1 text-xs text-text-3">
                          Devices seen by the selected host for this platform. This list updates automatically.
                        </p>
                      </div>
                      <span className="inline-flex items-center gap-1.5 rounded-full border border-success-strong/30 bg-success-soft px-2 py-0.5 text-xs font-medium text-success-foreground">
                        <span className={`h-1.5 w-1.5 rounded-full bg-success-strong ${isFetchingCandidates ? 'animate-pulse' : ''}`} />
                        Live
                      </span>
                    </div>
                    <div className="mt-3 flex flex-wrap items-center gap-3 text-xs text-text-3">
                      <span>{observedDeviceCountLabel}</span>
                      {showCandidateUpdate && (
                        <span className="font-medium text-accent-strong">Device list updated just now</span>
                      )}
                    </div>

                    {filteredCandidates.length > 0 ? (
                      <div className="mt-3">
                        <Field
                          label={candidateRequired ? 'Observed Device' : 'Observed Device (Optional)'}
                          htmlFor="add-device-candidate"
                        >
                          <Select
                            id="add-device-candidate"
                            value={selectedCandidateKey}
                            onChange={setSelectedCandidateKey}
                            placeholder={candidateRequired ? 'Select an observed device' : 'Use manual target instead'}
                            options={filteredCandidates.map((candidate) => {
                              const key = `${candidate.identity_value}:${candidate.connection_target ?? ''}`;
                              const label = candidate.already_registered
                                ? `${candidate.name} (${candidate.connection_target ?? candidate.identity_value}) - already registered`
                                : `${candidate.name} (${candidate.connection_target ?? candidate.identity_value})`;
                              return { value: key, label, disabled: candidate.already_registered };
                            })}
                            fullWidth
                          />
                        </Field>
                      </div>
                    ) : (
                      <p className="mt-3 rounded-md border border-dashed border-border px-3 py-2 text-xs text-text-3">
                        No matching devices observed yet
                      </p>
                    )}
                  </div>

                  {showIpField && (
                    <Field label="IP Address" htmlFor="add-device-ip">
                      <TextField
                        id="add-device-ip"
                        value={manualIpAddress}
                        onChange={setManualIpAddress}
                        placeholder="192.168.1.55"
                      />
                    </Field>
                  )}

                  {showConnectionTargetField && (
                    <Field
                      label="Connection Target"
                      htmlFor="add-device-connection-target"
                      hint="Host-visible address or identifier used by the selected driver."
                    >
                      <TextField
                        id="add-device-connection-target"
                        value={manualConnectionTarget}
                        onChange={setManualConnectionTarget}
                        placeholder="serial, UDID, host:port, or driver target"
                      />
                    </Field>
                  )}

                  {effectiveDescriptor && effectiveDescriptor.deviceFieldsSchema.length > 0 && (
                    <DeviceManifestFields
                      fields={effectiveDescriptor.deviceFieldsSchema}
                      value={deviceConfig}
                      onChange={setDeviceConfig}
                      idPrefix="add-device-config"
                    />
                  )}

                  <Field label="Display Name Override" htmlFor="add-device-name">
                    <TextField
                      id="add-device-name"
                      value={displayNameValue}
                      onChange={setDisplayName}
                      placeholder={selectedCandidate?.name ?? 'Optional override for the selected device'}
                    />
                  </Field>

                  <div className="rounded-md border border-border bg-surface-2 p-3 text-sm text-text-2">
                    <p className="font-medium text-text-1">Derived Device Data</p>
                    <p className="mt-2">Identity: {derivedIdentity}</p>
                    <p className="mt-1">Connection Target: {derivedConnectionTarget || 'Pending selection'}</p>
                    <p className="mt-1">OS Version: {derivedOsVersion}</p>
                    {candidateSummary && <p className="mt-1">Selected Device: {candidateSummary}</p>}
                  </div>
                </>
              )}
            </>
          )}
        </fieldset>

      </form>
    </Modal>
  );
}

type PlatformOptionDraft = {
  baseLabel: string;
  platformId: string;
  deviceTypes: string[];
  connectionTypes: string[];
};

const PLATFORM_DEVICE_TYPE_QUALIFIER_LABELS: Record<DeviceType, string> = {
  real_device: 'Real Device',
  emulator: 'Emulator',
  simulator: 'Simulator',
};

function platformOptionLabel(draft: PlatformOptionDraft, needsQualifier: boolean): string {
  if (!needsQualifier) return draft.baseLabel;
  const qualifier = platformOptionQualifier(draft);
  return qualifier ? `${draft.baseLabel} - ${qualifier}` : draft.baseLabel;
}

function platformOptionQualifier({ platformId, deviceTypes, connectionTypes }: PlatformOptionDraft): string | null {
  const deviceTypeLabels = deviceTypes
    .map((deviceType) => PLATFORM_DEVICE_TYPE_QUALIFIER_LABELS[deviceType as DeviceType] ?? null)
    .filter((label): label is string => label !== null);
  if (deviceTypeLabels.length > 0) return deviceTypeLabels.join(' / ');

  const connectionTypeLabels = connectionTypes
    .map((connectionType) => CONNECTION_TYPE_LABELS[connectionType as ConnectionType] ?? null)
    .filter((label): label is string => label !== null);
  if (connectionTypeLabels.length > 0) return connectionTypeLabels.join(' / ');

  const suffixMatch = platformId.match(/(?:^|[_-])(real(?:[_-]device)?|emulator|simulator|network|usb|virtual)$/i);
  if (!suffixMatch) return null;
  return resolvePlatformLabel(suffixMatch[1].replace(/[_-]/g, ' '), null);
}
