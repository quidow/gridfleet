import type { QueryClient } from '@tanstack/react-query';
import type {
  DesiredNodeState,
  DeviceDetail,
  DeviceRead,
} from '../types';
import type { PaginatedResponse } from '../types/shared';
import { qk } from './queryKeys';

type DeviceListData = DeviceRead[] | PaginatedResponse<DeviceRead>;
type DeviceQuerySnapshot = Array<[readonly unknown[], DeviceListData | undefined]>;

type OptimisticDeviceContext = {
  deviceId: string;
  devicesSnapshots: DeviceQuerySnapshot;
  deviceSnapshot: DeviceDetail | undefined;
};

type DeviceCacheUpdater = <T extends DeviceRead>(device: T) => T;

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

  qc.setQueryData(qk.device.detail(context.deviceId), context.deviceSnapshot);
}

export async function patchDeviceQueries(
  qc: QueryClient,
  deviceId: string,
  updater: DeviceCacheUpdater,
): Promise<OptimisticDeviceContext> {
  await Promise.all([
    qc.cancelQueries({ queryKey: qk.devices.root }),
    qc.cancelQueries({ queryKey: qk.device.detail(deviceId) }),
  ]);

  const devicesSnapshots = qc.getQueriesData<DeviceListData>({ queryKey: qk.devices.root });
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

  const deviceKey = qk.device.detail(deviceId);
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
  qc.invalidateQueries({ queryKey: qk.devices.root });
  qc.invalidateQueries({ queryKey: qk.device.detail(deviceId) });
}

export function updateOperationalState(
  operational_state: DeviceRead['operational_state'],
): DeviceCacheUpdater {
  return <T extends DeviceRead>(device: T): T => ({
    ...device,
    operational_state,
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
