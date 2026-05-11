import { useEffect, useRef } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { fetchDeviceVerificationJob } from '../../api/devices';
import { useAuth } from '../../context/auth';
import {
  CONNECTION_TYPE_LABELS,
  DEVICE_TYPE_LABELS,
} from '../../lib/deviceWorkflow';
import { platformDescriptorForDeviceType } from '../../lib/platformSelection';
import type {
  ConnectionType,
  DeviceVerificationCreate,
  DeviceRead,
  DeviceType,
  DeviceVerificationUpdate,
  DeviceVerificationJob,
  DeviceVerificationStageStatus,
  IntakeCandidate,
  PlatformDescriptor,

} from '../../types';

export { CONNECTION_TYPE_LABELS, DEVICE_TYPE_LABELS };

export const VERIFICATION_STAGE_LABELS: Record<string, string> = {
  validation: 'Validate Input',
  device_health: 'Check Device Health',
  node_start: 'Start Appium Node',
  session_probe: 'Probe Appium Session',
  cleanup: 'Clean Up Probe',
  save_device: 'Save Device',
};

const INITIAL_RECONNECT_DELAY_MS = 1_000;
const MAX_RECONNECT_DELAY_MS = 30_000;


export type HostOption = {
  id: string;
  name: string;
};

type VerificationConfigPreviewForm = {
  device_type?: DeviceType | null;
};

function getDevicePlatformId(device: DeviceRead): string {
  return device.platform_id;
}

export function getAllowedDeviceTypes(descriptor: PlatformDescriptor | null): DeviceType[] {
  return descriptor?.deviceTypes ?? [];
}

export function getAllowedConnectionTypes(
  descriptor: PlatformDescriptor | null,
  deviceType: DeviceType | null | undefined,
): ConnectionType[] {
  const connectionTypes = descriptor?.connectionTypes ?? [];
  if (deviceType !== undefined && deviceType !== null && deviceType !== 'real_device') {
    return connectionTypes.includes('virtual') ? ['virtual'] : [];
  }
  return connectionTypes.filter((connectionType) => connectionType !== 'virtual');
}

export function normalizeFormForDescriptor<T extends DeviceVerificationCreate | DeviceVerificationUpdate>(
  form: T,
  descriptor: PlatformDescriptor | null,
): T {
  if (!descriptor) return form;
  const next = { ...form };
  const allowedDeviceTypes = getAllowedDeviceTypes(descriptor);
  const currentDeviceType = (next.device_type as DeviceType | null | undefined) ?? allowedDeviceTypes[0];
  next.device_type = (
    allowedDeviceTypes.includes(currentDeviceType) ? currentDeviceType : allowedDeviceTypes[0]
  ) as T['device_type'];

  const allowedConnections = getAllowedConnectionTypes(descriptor, next.device_type as DeviceType | null | undefined);
  const currentConnection = next.connection_type as ConnectionType | null | undefined;
  next.connection_type = (
    allowedConnections.includes(currentConnection ?? allowedConnections[0])
      ? currentConnection ?? allowedConnections[0]
      : allowedConnections[0]
  ) as T['connection_type'];

  const currentConfig = (
    next.device_config && typeof next.device_config === 'object' ? next.device_config : {}
  ) as Record<string, unknown>;
  const nextConfig = { ...currentConfig };
  const effectiveDescriptor = platformDescriptorForDeviceType(descriptor, next.device_type as DeviceType | null | undefined);
  for (const field of effectiveDescriptor?.deviceFieldsSchema ?? []) {
    if (field.default !== undefined && nextConfig[field.id] === undefined) {
      nextConfig[field.id] = field.default;
    }
  }
  if (Object.keys(nextConfig).length > 0) {
    next.device_config = nextConfig as T['device_config'];
  }

  return next;
}

export function buildExistingVerificationForm(
  device: DeviceRead,
  descriptor: PlatformDescriptor | null,
): DeviceVerificationUpdate {
  const platformId = getDevicePlatformId(device);
  return normalizeFormForDescriptor(
    {
      pack_id: device.pack_id,
      platform_id: platformId,
      identity_scheme: device.identity_scheme,
      identity_scope: device.identity_scope,
      identity_value: device.identity_value,
      connection_target: device.connection_target,
      name: device.name,
      os_version: device.os_version,
      host_id: device.host_id,
      device_type: device.device_type,
      connection_type: device.connection_type,
      ip_address: device.ip_address,
    },
    descriptor,
  );
}

export function showDeviceTypeField(
  descriptor: PlatformDescriptor | null,
): boolean {
  return getAllowedDeviceTypes(descriptor).length > 1;
}

export function showConnectionTypeField(
  descriptor: PlatformDescriptor | null,
  deviceType: DeviceType | null | undefined,
): boolean {
  return getAllowedConnectionTypes(descriptor, deviceType).length > 1;
}

export function showIpAddressField(form: DeviceVerificationCreate | DeviceVerificationUpdate): boolean {
  return form.connection_type === 'network';
}

export function showOsVersionField(descriptor: PlatformDescriptor | null): boolean {
  return descriptor !== null && descriptor.deviceFieldsSchema.every((field) => field.id !== 'no_os_version');
}

export function generatedConfigPreview(
  form: VerificationConfigPreviewForm,
  descriptor: PlatformDescriptor | null,
): string[] {
  const preview: string[] = [];
  if (!descriptor) {
    return preview;
  }
  if (form.device_type && form.device_type !== 'real_device') {
    preview.push('Connection type will be fixed to Virtual.');
  }
  const effectiveDescriptor = platformDescriptorForDeviceType(descriptor, form.device_type);
  if (Object.keys(effectiveDescriptor?.defaultCapabilities ?? {}).length > 0) {
    preview.push('Manifest default capabilities will be applied during verification.');
  }
  return preview;
}

export function buildExistingVerificationPayload(
  form: DeviceVerificationUpdate,
  device: DeviceRead,
  descriptor: PlatformDescriptor | null,
): DeviceVerificationUpdate {
  const platformId = form.platform_id ?? getDevicePlatformId(device);
  const normalized = normalizeFormForDescriptor({ ...form, platform_id: platformId }, descriptor);
  return {
    ...normalized,
    identity_value: normalized.identity_value ?? device.identity_value,
    connection_target: normalized.connection_target ?? normalized.ip_address ?? device.connection_target,
    os_version: showOsVersionField(descriptor) ? normalized.os_version : 'unknown',
  };
}

export function parseIpAddress(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  if (trimmed.includes(':')) return trimmed.split(':', 1)[0] || null;
  return trimmed;
}

export function manualRegistrationRequirements(
  descriptor: PlatformDescriptor | null,
  deviceType: DeviceType | null | undefined,
  connectionType: ConnectionType | null | undefined,
): { identity: boolean; connectionTarget: boolean; ipAddress: boolean } {
  if (!descriptor) {
    return { identity: false, connectionTarget: false, ipAddress: false };
  }
  if (connectionType !== 'network') {
    return { identity: false, connectionTarget: false, ipAddress: false };
  }
  const effectiveDescriptor = platformDescriptorForDeviceType(descriptor, deviceType);
  const behavior = effectiveDescriptor?.connectionBehavior ?? descriptor.connectionBehavior;
  const requiresIpAddress =
    typeof behavior.requires_ip_address === 'boolean'
      ? behavior.requires_ip_address
      : connectionType === 'network';
  return {
    identity: false,
    connectionTarget: behavior.requires_connection_target !== false,
    ipAddress: requiresIpAddress,
  };
}

export function laneNeedsCandidate(
  descriptor: PlatformDescriptor | null,
  deviceType: DeviceType,
  connectionType: ConnectionType,
): boolean {
  const requirements = manualRegistrationRequirements(descriptor, deviceType, connectionType);
  return !requirements.identity && !requirements.connectionTarget && !requirements.ipAddress;
}

export function filterIntakeCandidates(
  candidates: IntakeCandidate[],
  descriptor: PlatformDescriptor | null,
  deviceType: DeviceType,
  connectionType: ConnectionType,
): IntakeCandidate[] {
  if (!descriptor) return [];
  return candidates.filter((candidate) => {
    if (candidate.platform_id !== descriptor.platformId) return false;
    if (candidate.device_type !== deviceType) return false;
    const effectiveDescriptor = platformDescriptorForDeviceType(descriptor, deviceType);
    if (effectiveDescriptor?.identityScope === 'global') return true;
    if (deviceType !== 'real_device') return true;
    return candidate.connection_type === connectionType;
  });
}

export function verificationStatusClasses(status: DeviceVerificationStageStatus): string {
  switch (status) {
    case 'passed':
      return 'border-success-strong/30 bg-success-soft text-success-foreground';
    case 'failed':
      return 'border-danger-strong/30 bg-danger-soft text-danger-foreground';
    case 'running':
      return 'border-info-strong/30 bg-info-soft text-info-foreground';
    case 'skipped':
      return 'border-warning-strong/30 bg-warning-soft text-warning-foreground';
    default:
      return 'border-border bg-surface-2 text-text-3';
  }
}

export function verificationStatusLabel(status: DeviceVerificationStageStatus): string {
  switch (status) {
    case 'passed':
      return 'Passed';
    case 'failed':
      return 'Failed';
    case 'running':
      return 'Running';
    case 'skipped':
      return 'Skipped';
    default:
      return 'Pending';
  }
}

type UseDeviceVerificationJobControllerArgs = {
  isOpen: boolean;
  isStarting?: boolean;
  job: DeviceVerificationJob | null;
  onJobChange: (job: DeviceVerificationJob) => void;
  onCompleted?: () => void;
  onClose: () => void;
  extraInvalidationKeys?: ReadonlyArray<readonly unknown[]>;
};

export function useDeviceVerificationJobController({
  isOpen,
  isStarting = false,
  job,
  onJobChange,
  onCompleted,
  onClose,
  extraInvalidationKeys = [],
}: UseDeviceVerificationJobControllerArgs) {
  const queryClient = useQueryClient();
  const auth = useAuth();
  const handledCompletedVerificationRef = useRef<string | null>(null);
  const reconnectDelayRef = useRef(INITIAL_RECONNECT_DELAY_MS);
  const jobId = job?.job_id ?? null;
  const activeJob = job;
  const isVerificationRunning =
    isStarting || activeJob?.status === 'pending' || activeJob?.status === 'running';

  useEffect(() => {
    if (!activeJob) return;
    if (activeJob.status === 'completed' && activeJob.job_id !== handledCompletedVerificationRef.current) {
      handledCompletedVerificationRef.current = activeJob.job_id;
      queryClient.invalidateQueries({ queryKey: ['devices'] });
      for (const queryKey of extraInvalidationKeys) {
        queryClient.invalidateQueries({ queryKey });
      }
      onCompleted?.();
      onClose();
      return;
    }
    if (activeJob.status === 'failed') {
      handledCompletedVerificationRef.current = null;
    }
  }, [activeJob, extraInvalidationKeys, onClose, onCompleted, queryClient]);

  useEffect(() => {
    if (!isOpen || !jobId || !isVerificationRunning) return;

    let disposed = false;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let eventSource: EventSource | null = null;

    const closeEventSource = () => {
      if (eventSource) {
        eventSource.close();
        eventSource = null;
      }
    };

    const onVerificationEvent = (event: MessageEvent) => {
      let parsed: DeviceVerificationJob;
      try {
        parsed = JSON.parse(event.data) as DeviceVerificationJob;
      } catch {
        return;
      }
      if (parsed.job_id !== jobId) return;
      onJobChange(parsed);
      if (parsed.status === 'completed' || parsed.status === 'failed') {
        closeEventSource();
      }
    };

    const scheduleReconnect = async () => {
      closeEventSource();

      const authSession = await auth.probeSession();
      if (disposed || (authSession.enabled && !authSession.authenticated)) {
        return;
      }

      try {
        const latestJob = await fetchDeviceVerificationJob(jobId);
        if (disposed) return;
        onJobChange(latestJob);
        if (latestJob.status === 'completed' || latestJob.status === 'failed') {
          return;
        }
      } catch {
        if (disposed) return;
      }

      const delay = reconnectDelayRef.current;
      reconnectDelayRef.current = Math.min(reconnectDelayRef.current * 2, MAX_RECONNECT_DELAY_MS);
      reconnectTimer = window.setTimeout(connect, delay);
    };

    const connect = () => {
      if (disposed) return;
      closeEventSource();

      eventSource = new EventSource(`/api/devices/verification-jobs/${jobId}/events`);
      eventSource.onopen = () => {
        reconnectDelayRef.current = INITIAL_RECONNECT_DELAY_MS;
      };
      eventSource.onerror = () => {
        if (disposed) return;
        void scheduleReconnect();
      };
      eventSource.addEventListener('device.verification.updated', onVerificationEvent as EventListener);
    };

    reconnectDelayRef.current = INITIAL_RECONNECT_DELAY_MS;
    connect();

    return () => {
      disposed = true;
      if (reconnectTimer) {
        window.clearTimeout(reconnectTimer);
      }
      if (eventSource) {
        eventSource.removeEventListener('device.verification.updated', onVerificationEvent as EventListener);
      }
      closeEventSource();
    };
  }, [auth, isOpen, isVerificationRunning, jobId, onJobChange]);

  return {
    activeJob,
    isVerificationRunning,
    resetCompletionGuard() {
      handledCompletedVerificationRef.current = null;
    },
  };
}
