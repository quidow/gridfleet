import api from './client';
import type { DriverPack, DriverPackHostsResponse, DriverPackReleasesResponse } from '../types/driverPacks';

export async function fetchDriverPack(packId: string): Promise<DriverPack> {
  const { data } = await api.get<DriverPack>(`/driver-packs/${encodeURIComponent(packId)}`);
  return data;
}

export async function fetchDriverPackReleases(packId: string): Promise<DriverPackReleasesResponse> {
  const { data } = await api.get<DriverPackReleasesResponse>(
    `/driver-packs/${encodeURIComponent(packId)}/releases`,
  );
  return data;
}

export async function fetchDriverPackHosts(packId: string): Promise<DriverPackHostsResponse> {
  const { data } = await api.get<DriverPackHostsResponse>(
    `/driver-packs/${encodeURIComponent(packId)}/hosts`,
  );
  return data;
}

export async function deleteDriverPack(packId: string): Promise<void> {
  await api.delete(`/driver-packs/${encodeURIComponent(packId)}`);
}

export async function setDriverPackCurrentRelease(packId: string, release: string): Promise<DriverPack> {
  const { data } = await api.patch<DriverPack>(`/driver-packs/${encodeURIComponent(packId)}/releases/current`, {
    release,
  });
  return data;
}
