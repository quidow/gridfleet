import { afterEach, describe, expect, it } from 'vitest';
import type { AxiosAdapter, AxiosResponse } from 'axios';

import api from './client';

const originalAdapter = api.defaults.adapter;

afterEach(() => {
  api.defaults.adapter = originalAdapter;
});

describe('api client request payload headers', () => {
  it('sends FormData as multipart data instead of JSON', async () => {
    let capturedData: unknown;
    let capturedContentType: string | undefined;
    const adapter: AxiosAdapter = async (config): Promise<AxiosResponse> => {
      capturedData = config.data;
      capturedContentType = config.headers.get('Content-Type') ?? undefined;
      return {
        data: {},
        status: 200,
        statusText: 'OK',
        headers: {},
        config,
      };
    };
    api.defaults.adapter = adapter;

    const form = new FormData();
    form.append('tarball', new File(['bytes'], 'driver.tar.gz', { type: 'application/gzip' }));

    await api.post('/driver-packs/uploads', form);

    expect(capturedData).toBe(form);
    expect(capturedContentType).not.toBe('application/json');
  });
});
