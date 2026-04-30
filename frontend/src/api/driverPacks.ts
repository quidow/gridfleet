import api from './client';
import type { DriverPack, HostDriverPacksStatus, RuntimePolicy } from '../types/driverPacks';

export async function fetchDriverPackCatalog(): Promise<DriverPack[]> {
  const { data } = await api.get<{ packs: DriverPack[] }>('/driver-packs/catalog');
  return data.packs;
}

export async function setDriverPackState(
  packId: string,
  state: 'enabled' | 'disabled',
): Promise<DriverPack> {
  const { data } = await api.patch<DriverPack>(`/driver-packs/${packId}`, { state });
  return data;
}

export async function setDriverPackPolicy(packId: string, runtimePolicy: RuntimePolicy): Promise<DriverPack> {
  const { data } = await api.patch<DriverPack>(`/driver-packs/${packId}/policy`, {
    runtime_policy: runtimePolicy,
  });
  return data;
}

export async function fetchHostDriverPacks(hostId: string): Promise<HostDriverPacksStatus> {
  const { data } = await api.get<HostDriverPacksStatus>(`/hosts/${hostId}/driver-packs`);
  return data;
}
