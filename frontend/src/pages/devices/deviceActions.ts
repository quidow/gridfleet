import type { DeviceRead } from '../../types';

export type DeviceAction =
  | { type: 'enter-maintenance'; deviceId: string }
  | { type: 'exit-maintenance'; deviceId: string }
  | { type: 'start-node'; deviceId: string }
  | { type: 'stop-node'; deviceId: string }
  | { type: 'restart-node'; deviceId: string }
  | { type: 'verify'; device: DeviceRead }
  | { type: 'edit'; device: DeviceRead }
  | { type: 'delete'; deviceId: string }
  | { type: 'toggle-auto-manage'; deviceId: string; autoManage: boolean };
