import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  testMatch: [
    'live-smoke.spec.ts',
  ],
  timeout: 30_000,
  retries: 0,
  workers: 1,
  use: {
    baseURL: 'http://127.0.0.1:3000',
    headless: true,
  },
  projects: [
    { name: 'chromium', use: { browserName: 'chromium' } },
  ],
});
