import { defineConfig } from '@playwright/test';

const frontendUrl = 'http://127.0.0.1:5173';
const workers = process.env.E2E_WORKERS ? Number(process.env.E2E_WORKERS) : undefined;

export default defineConfig({
  testDir: './e2e',
  testIgnore: [
    'live-smoke.spec.ts',
    'a11y.spec.ts',
  ],
  timeout: 30_000,
  retries: 0,
  fullyParallel: true,
  workers,
  use: {
    baseURL: frontendUrl,
    headless: true,
  },
  projects: [
    { name: 'chromium', use: { browserName: 'chromium' } },
  ],
  webServer: [
    {
      command: 'npm run dev -- --host 127.0.0.1 --port 5173',
      cwd: '.',
      url: frontendUrl,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
  ],
});
