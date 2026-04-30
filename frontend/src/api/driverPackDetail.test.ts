import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  deleteDriverPack,
  deleteDriverPackRelease,
  fetchDriverPackHosts,
  fetchDriverPackReleases,
  setDriverPackCurrentRelease,
} from './driverPackDetail';
import api from './client';

vi.mock('./client', () => ({
  default: { delete: vi.fn(), get: vi.fn(), patch: vi.fn() },
}));

describe('driver pack detail api', () => {
  beforeEach(() => {
    (api.get as ReturnType<typeof vi.fn>).mockReset();
    (api.delete as ReturnType<typeof vi.fn>).mockReset();
    (api.patch as ReturnType<typeof vi.fn>).mockReset();
  });

  it('fetches releases for a pack', async () => {
    (api.get as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: {
        pack_id: 'appium-roku-dlenroc',
        releases: [{ release: '2026.04.1', is_current: true, artifact_sha256: 'sha', platform_ids: [] }],
      },
    });

    const result = await fetchDriverPackReleases('appium-roku-dlenroc');

    expect(api.get).toHaveBeenCalledWith('/driver-packs/appium-roku-dlenroc/releases');
    expect(result.releases[0].release).toBe('2026.04.1');
  });

  it('switches the current release for a pack', async () => {
    (api.patch as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: { id: 'appium-roku-dlenroc', current_release: '2026.04.0' },
    });

    const result = await setDriverPackCurrentRelease('appium-roku-dlenroc', '2026.04.0');

    expect(api.patch).toHaveBeenCalledWith('/driver-packs/appium-roku-dlenroc/releases/current', {
      release: '2026.04.0',
    });
    expect(result.current_release).toBe('2026.04.0');
  });

  it('deletes a specific release', async () => {
    (api.delete as ReturnType<typeof vi.fn>).mockResolvedValue({ data: undefined });

    await deleteDriverPackRelease('appium-roku-dlenroc', '2026.04.0');

    expect(api.delete).toHaveBeenCalledWith('/driver-packs/appium-roku-dlenroc/releases/2026.04.0');
  });

  it('deletes a driver pack', async () => {
    (api.delete as ReturnType<typeof vi.fn>).mockResolvedValue({ data: undefined });

    await deleteDriverPack('appium-roku-dlenroc');

    expect(api.delete).toHaveBeenCalledWith('/driver-packs/appium-roku-dlenroc');
  });

  it('fetches host status for a pack', async () => {
    (api.get as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: {
        pack_id: 'appium-xcuitest',
        hosts: [{ host_id: 'host-1', hostname: 'mac-host', status: 'online' }],
      },
    });

    const result = await fetchDriverPackHosts('appium-xcuitest');

    expect(api.get).toHaveBeenCalledWith('/driver-packs/appium-xcuitest/hosts');
    expect(result.hosts[0].hostname).toBe('mac-host');
  });
});
