import type { DevicePatch, DeviceRead, DeviceReadinessState } from '../types';

export function readinessLabel(state: DeviceReadinessState): string {
  switch (state) {
    case 'setup_required':
      return 'Setup Required';
    case 'verification_required':
      return 'Needs Verification';
    default:
      return 'Verified';
  }
}

export function missingSetupFieldLabel(field: string, labels?: Map<string, string>): string {
  return labels?.get(field) ?? field.replaceAll('_', ' ');
}

export function buildDeviceFieldLabelMap(fields: Array<{ id: string; label: string }>): Map<string, string> {
  return new Map(fields.map((field) => [field.id, field.label]));
}

export function deviceUpdateRequiresReverification(
  device: DeviceRead,
  body: DevicePatch,
  readinessFields: string[] = [],
): boolean {
  const fields = new Set(['connection_target', 'ip_address']);
  for (const field of fields) {
    if (!(field in device)) {
      if ((body as Record<string, unknown>)[field] !== undefined) return true;
      continue;
    }
    if (field in body && (body as Record<string, unknown>)[field] !== device[field as keyof DeviceRead]) return true;
  }
  if (readinessFields.length > 0 && body.device_config !== undefined) {
    const currentConfig = device.device_config ?? {};
    const nextConfig = body.device_config ?? {};
    for (const field of readinessFields) {
      if (field in nextConfig && nextConfig[field] !== currentConfig[field]) return true;
    }
  }
  return false;
}

export const READINESS_GLOSSARY = {
  identity: 'Identity is the stable device identity you use to recognize the same hardware over time.',
  connectionTarget: 'Connection Target is the current transport or Appium route the manager uses right now.',
  setupRequired: 'Setup Required means operator-supplied inputs are still missing.',
  verificationRequired: 'Needs Verification means setup is present, but the current configuration must be re-probed.',
};
