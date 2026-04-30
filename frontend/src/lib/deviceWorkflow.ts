import type { ConnectionType, DeviceReadinessState, DeviceType } from '../types';

export const DEVICE_TYPE_OPTIONS: DeviceType[] = ['real_device', 'emulator', 'simulator'];
export const CONNECTION_TYPE_OPTIONS: ConnectionType[] = ['usb', 'network', 'virtual'];

export const DEVICE_TYPE_LABELS: Record<DeviceType, string> = {
  real_device: 'Real',
  emulator: 'Emulator',
  simulator: 'Simulator',
};

export const CONNECTION_TYPE_LABELS: Record<ConnectionType, string> = {
  usb: 'USB',
  network: 'Network',
  virtual: 'Virtual',
};

type VerificationActionPresentation = {
  title: string;
  buttonLabel: string;
  handoffMessage?: string;
};

export function getVerificationAction(readinessState: DeviceReadinessState): VerificationActionPresentation {
  if (readinessState === 'setup_required') {
    return {
      title: 'Complete Setup',
      buttonLabel: 'Complete Setup',
    };
  }

  if (readinessState === 'verified') {
    return {
      title: 'Re-verify Device',
      buttonLabel: 'Re-verify',
      handoffMessage: 'Run guided verification again for the current saved configuration.',
    };
  }

  return {
    title: 'Verify Device',
    buttonLabel: 'Verify Device',
  };
}

export function getDiscoveryImportActionLabel(readinessState: DeviceReadinessState): string {
  return readinessState === 'setup_required' ? 'Import & Complete Setup' : 'Import & Verify';
}
