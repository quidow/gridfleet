import api from './client';
import type { DriverPack } from '../types/driverPacks';

export async function uploadDriverPack(file: File, displayHint?: string): Promise<DriverPack> {
  const form = new FormData();
  form.append('tarball', file);
  if (displayHint) {
    form.append('display_hint', displayHint);
  }
  const { data } = await api.post<DriverPack>('/driver-packs/uploads', form);
  return data;
}

export async function forkDriverPack(
  sourcePackId: string,
  body: { new_pack_id: string; display_name?: string },
): Promise<DriverPack> {
  const { data } = await api.post<DriverPack>(
    `/driver-packs/${encodeURIComponent(sourcePackId)}/fork`,
    body,
  );
  return data;
}

export async function exportPack(packId: string, release: string): Promise<void> {
  const res = await api.post(
    `/driver-packs/${encodeURIComponent(packId)}/releases/${encodeURIComponent(release)}/export`,
    null,
    { responseType: 'blob' },
  );
  const blob = new Blob([res.data as BlobPart], { type: 'application/gzip' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${packId.replace('/', '_')}-${release}.tar.gz`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
