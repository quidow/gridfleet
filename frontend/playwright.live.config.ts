import { defineConfig } from '@playwright/test';

const backendUrl = 'http://127.0.0.1:8000';
const frontendUrl = 'http://127.0.0.1:5173';

export default defineConfig({
  testDir: './e2e',
  testMatch: [
    'live-smoke.spec.ts',
    'a11y.spec.ts',
  ],
  timeout: 30_000,
  retries: 0,
  workers: 1,
  use: {
    baseURL: frontendUrl,
    headless: true,
  },
  projects: [
    { name: 'chromium', use: { browserName: 'chromium' } },
  ],
  webServer: [
    {
      command: 'uv run uvicorn app.main:app --host 127.0.0.1 --port 8000',
      cwd: '../backend',
      url: `${backendUrl}/health/live`,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    {
      command: 'npm run dev -- --host 127.0.0.1 --port 5173',
      cwd: '.',
      url: frontendUrl,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
  ],
});
