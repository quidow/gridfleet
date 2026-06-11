import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fetchHealth } from './grid';
import api from './client';

vi.mock('./client', () => ({
  default: { get: vi.fn() },
}));

const mockedGet = api.get as ReturnType<typeof vi.fn>;

describe('fetchHealth', () => {
  beforeEach(() => {
    mockedGet.mockReset();
  });

  it('accepts the degraded 503 payload as data instead of an error', async () => {
    mockedGet.mockResolvedValue({
      status: 503,
      data: { status: 'unhealthy', checks: { database: 'connection refused' } },
    });

    const result = await fetchHealth();

    expect(result.status).toBe('unhealthy');
    expect(result.checks.database).toBe('connection refused');

    // The degraded signal must not be treated as a transport error by axios.
    const config = mockedGet.mock.calls[0]![1] as { validateStatus?: (status: number) => boolean } | undefined;
    expect(config?.validateStatus).toBeDefined();
    expect(config!.validateStatus!(200)).toBe(true);
    expect(config!.validateStatus!(503)).toBe(true);
    expect(config!.validateStatus!(500)).toBe(false);
    expect(config!.validateStatus!(401)).toBe(false);
  });
});
