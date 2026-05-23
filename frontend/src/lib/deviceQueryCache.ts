import type { QueryClient } from '@tanstack/react-query';
import type {
  DesiredNodeState,
  DeviceDetail,
  DeviceRead,
} from '../types';
import type { PaginatedResponse } from '../types/shared';

export type DeviceListData = DeviceRead[] | PaginatedResponse<DeviceRead>;
export type DeviceQuerySnapshot = Array<[readonly unknown[], DeviceListData | undefined]>;

export type OptimisticDeviceContext = {
  deviceId: string;
  devicesSnapshots: DeviceQuerySnapshot;
  deviceSnapshot: DeviceDetail | undefined;
};

export type DeviceCacheUpdater = <T extends DeviceRead>(device: T) => T;

export function rollbackOptimisticDeviceQueries(
  qc: QueryClient,
  context: OptimisticDeviceContext | undefined,
) {
  if (!context) {
    return;
  }

  for (const [queryKey, snapshot] of context.devicesSnapshots) {
    qc.setQueryData<DeviceListData>(queryKey, snapshot);
  }

  qc.setQueryData(['device', context.deviceId], context.deviceSnapshot);
}

export async function patchDeviceQueries(
  qc: QueryClient,
  deviceId: string,
  updater: DeviceCacheUpdater,
): Promise<OptimisticDeviceContext> {
  await Promise.all([
    qc.cancelQueries({ queryKey: ['devices'] }),
    qc.cancelQueries({ queryKey: ['device', deviceId] }),
  ]);

  const devicesSnapshots = qc.getQueriesData<DeviceListData>({ queryKey: ['devices'] });
  for (const [queryKey, snapshot] of devicesSnapshots) {
    if (!snapshot) {
      continue;
    }
    if (Array.isArray(snapshot)) {
      qc.setQueryData<DeviceRead[]>(
        queryKey,
        snapshot.map((device) => (device.id === deviceId ? updater(device) : device)),
      );
    } else {
      qc.setQueryData<PaginatedResponse<DeviceRead>>(queryKey, {
        ...snapshot,
        items: snapshot.items.map((device) => (device.id === deviceId ? updater(device) : device)),
      });
    }
  }

  const deviceKey = ['device', deviceId] as const;
  const deviceSnapshot = qc.getQueryData<DeviceDetail>(deviceKey);
  if (deviceSnapshot) {
    qc.setQueryData<DeviceDetail>(deviceKey, updater(deviceSnapshot));
  }

  return {
    deviceId,
    devicesSnapshots,
    deviceSnapshot,
  };
}

export function invalidatePatchedDeviceQueries(
  qc: QueryClient,
  deviceId: string,
) {
  qc.invalidateQueries({ queryKey: ['devices'] });
  qc.invalidateQueries({ queryKey: ['device', deviceId] });
}

export function updateHold(hold: DeviceRead['hold']): DeviceCacheUpdater {
  return <T extends DeviceRead>(device: T): T => ({
    ...device,
    hold,
  });
}

export function updateEmulatorState(state: string): DeviceCacheUpdater {
  return <T extends DeviceRead>(device: T): T => ({
    ...device,
    emulator_state: state,
  });
}

export function updateNodeOperationalState(
  operationalState: DeviceRead['operational_state'],
  nodeState: DesiredNodeState,
): DeviceCacheUpdater {
  return <T extends DeviceRead>(device: T): T => {
    const nextDevice = {
      ...device,
      operational_state: operationalState,
    } as T & Partial<DeviceDetail>;

    if (nextDevice.appium_node) {
      nextDevice.appium_node = {
        ...nextDevice.appium_node,
        desired_state: nodeState,
        effective_state: nodeState === 'running' ? 'starting' : 'stopped',
        pid: nodeState === 'running' ? nextDevice.appium_node.pid : null,
      };
    }

    return nextDevice as T;
  };
}
