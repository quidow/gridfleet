import { beforeEach, describe, expect, it, vi } from 'vitest';
import { invokeFeatureAction } from './hostFeatureActions';
import api from './client';

vi.mock('./client', () => ({
  default: { post: vi.fn() },
}));

describe('invokeFeatureAction', () => {
  beforeEach(() => {
    (api.post as ReturnType<typeof vi.fn>).mockReset();
  });

  it('POSTs to the correct URL and returns the result', async () => {
    (api.post as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: { ok: true, detail: 'action succeeded', data: {} },
    });

    const result = await invokeFeatureAction('host-1', 'pack-a', 'feat-x', 'run', { key: 'value' });

    expect(api.post).toHaveBeenCalledWith(
      '/hosts/host-1/driver-packs/pack-a/features/feat-x/actions/run',
      { args: { key: 'value' } },
    );
    expect(result.ok).toBe(true);
    expect(result.detail).toBe('action succeeded');
  });

  it('defaults args to empty object when not provided', async () => {
    (api.post as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: { ok: true, detail: '', data: {} },
    });

    await invokeFeatureAction('host-1', 'pack-a', 'feat-x', 'run');

    expect(api.post).toHaveBeenCalledWith(
      '/hosts/host-1/driver-packs/pack-a/features/feat-x/actions/run',
      { args: {} },
    );
  });

  it('propagates errors from the API', async () => {
    const error = new Error('Request failed');
    (api.post as ReturnType<typeof vi.fn>).mockRejectedValue(error);

    await expect(invokeFeatureAction('host-1', 'pack-a', 'feat-x', 'run')).rejects.toThrow('Request failed');
  });

  it('returns ok=false and detail on failure response', async () => {
    (api.post as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: { ok: false, detail: 'agent unreachable', data: {} },
    });

    const result = await invokeFeatureAction('host-1', 'pack-a', 'feat-x', 'run');

    expect(result.ok).toBe(false);
    expect(result.detail).toBe('agent unreachable');
  });
});
