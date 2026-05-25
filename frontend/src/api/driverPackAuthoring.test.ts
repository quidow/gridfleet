import { beforeEach, describe, expect, it, vi } from 'vitest';

import { uploadDriverPack } from './driverPackAuthoring';
import api from './client';

vi.mock('./client', () => ({
  default: { post: vi.fn() },
}));

describe('driver pack authoring api', () => {
  beforeEach(() => {
    (api.post as ReturnType<typeof vi.fn>).mockReset();
  });

  it('POSTs a FormData to /driver-packs/uploads for uploadDriverPack', async () => {
    (api.post as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: { id: 'vendor-foo', state: 'enabled' },
    });
    const file = new File(['bytes'], 'driver.tar.gz', { type: 'application/gzip' });
    const pack = await uploadDriverPack(file);
    expect(api.post).toHaveBeenCalledOnce();
    const [url, body] = (api.post as ReturnType<typeof vi.fn>).mock.calls[0] as [string, FormData];
    expect(url).toBe('/driver-packs/uploads');
    expect(body).toBeInstanceOf(FormData);
    expect(body.get('tarball')).toBe(file);
    expect(pack.id).toBe('vendor-foo');
  });

  it('includes display_hint in FormData when provided', async () => {
    (api.post as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: { id: 'vendor-bar', state: 'enabled' },
    });
    const file = new File(['bytes'], 'driver.tar.gz', { type: 'application/gzip' });
    await uploadDriverPack(file, 'My Custom Driver');
    const [, body] = (api.post as ReturnType<typeof vi.fn>).mock.calls[0] as [string, FormData];
    expect(body.get('display_hint')).toBe('My Custom Driver');
  });
});
