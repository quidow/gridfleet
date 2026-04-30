export type DevicePendingAction =
  | 'starting'
  | 'stopping'
  | 'restarting'
  | 'running-lifecycle-action'
  | 'entering-maintenance'
  | 'exiting-maintenance'
  | 'updating-auto-manage';

type PendingDeviceMutation = {
  action: DevicePendingAction;
  isPending: boolean;
  deviceId?: string | null;
};

export function getPendingDeviceAction(
  deviceId: string,
  mutations: PendingDeviceMutation[],
): DevicePendingAction | null {
  for (const mutation of mutations) {
    if (mutation.isPending && mutation.deviceId === deviceId) {
      return mutation.action;
    }
  }
  return null;
}

export function getPendingDeviceActionLabel(
  pendingAction: DevicePendingAction | null,
): string | null {
  switch (pendingAction) {
    case 'starting':
      return 'Starting...';
    case 'stopping':
      return 'Stopping...';
    case 'restarting':
      return 'Restarting...';
    case 'running-lifecycle-action':
      return 'Running lifecycle action...';
    case 'entering-maintenance':
      return 'Entering maintenance...';
    case 'exiting-maintenance':
      return 'Exiting maintenance...';
    case 'updating-auto-manage':
      return 'Saving auto-manage...';
    default:
      return null;
  }
}
