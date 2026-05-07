import { test, expect } from './helpers/fixtures';
import { fulfillJson, mockEmptySettingsApi, mockEventsApi } from './helpers/routes';

const RUN_ID = 'run-detail-001';

const ACTIVE_DEVICE = {
  device_id: 'device-abc',
  identity_value: 'android-abc',
  connection_target: '10.0.0.10:5555',
  pack_id: 'appium-uiautomator2',
  platform_id: 'android_mobile',
  platform_label: 'Android (real device)',
  os_version: '14',
  host_ip: '10.0.0.5',
  excluded: false,
  exclusion_reason: null,
  excluded_at: null,
  excluded_until: null,
  cooldown_remaining_sec: null,
  cooldown_count: 0,
};

const ESCALATED_DEVICE = {
  ...ACTIVE_DEVICE,
  excluded: true,
  exclusion_reason: 'cooldown threshold exceeded',
  excluded_at: new Date(Date.UTC(2026, 3, 10, 10, 6, 0)).toISOString(),
  excluded_until: null,
  cooldown_remaining_sec: null,
  cooldown_count: 3,
};

const MOCK_RUN = {
  id: RUN_ID,
  name: 'my-ci-run',
  state: 'active',
  requirements: [{ pack_id: 'appium-uiautomator2', platform_id: 'android_mobile', count: 1 }],
  ttl_minutes: 60,
  heartbeat_timeout_sec: 120,
  reserved_devices: [ACTIVE_DEVICE],
  devices: [ACTIVE_DEVICE],
  error: null,
  created_at: new Date(Date.UTC(2026, 3, 10, 10, 0, 0)).toISOString(),
  started_at: new Date(Date.UTC(2026, 3, 10, 10, 1, 0)).toISOString(),
  completed_at: null,
  created_by: 'github/actions',
  last_heartbeat: new Date(Date.UTC(2026, 3, 10, 10, 5, 0)).toISOString(),
  session_counts: { passed: 0, failed: 0, error: 0, running: 0, total: 0 },
};

const MOCK_RUN_ESCALATED = {
  ...MOCK_RUN,
  reserved_devices: [ESCALATED_DEVICE],
  devices: [ESCALATED_DEVICE],
};

const MOCK_SESSION = {
  id: 'sess-001',
  session_id: 'abcd1234efgh5678',
  device_id: 'device-abc',
  device_name: 'Pixel 8',
  device_pack_id: 'appium-uiautomator2',
  device_platform_id: 'android_mobile',
  device_platform_label: 'Android (real device)',
  test_name: 'test_checkout',
  started_at: new Date(Date.UTC(2026, 3, 10, 10, 2, 0)).toISOString(),
  ended_at: new Date(Date.UTC(2026, 3, 10, 10, 3, 30)).toISOString(),
  status: 'passed',
  requested_pack_id: null,
  requested_platform_id: null,
  requested_device_type: null,
  requested_connection_type: null,
  requested_capabilities: null,
  error_type: null,
  error_message: null,
  run_id: RUN_ID,
};

async function setupCommonRoutes(page: Parameters<typeof test>[1]['page']) {
  await mockEventsApi(page);
  await mockEmptySettingsApi(page);
  await page.route(`**/api/runs/${RUN_ID}`, async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    await fulfillJson(route, MOCK_RUN);
  });
}

test.describe('RunDetail sessions panel', () => {
  test('shows sessions belonging to the run', async ({ page }) => {
    await setupCommonRoutes(page);

    await page.route((url) => new URL(url).pathname === '/api/sessions', async (route) => {
      await fulfillJson(route, {
        items: [MOCK_SESSION],
        total: 1,
        limit: 50,
        offset: 0,
        next_cursor: null,
        prev_cursor: null,
      });
    });

    await page.goto(`/runs/${RUN_ID}`);
    await expect(page.getByRole('heading', { name: 'my-ci-run' })).toBeVisible({ timeout: 15_000 });

    // Sessions panel header should show count
    await expect(page.getByText(/Sessions \(1\)/)).toBeVisible();

    // Session row data
    await expect(page.getByText('test_checkout')).toBeVisible();
    // session_id is 16 chars (≤18), shown untruncated
    await expect(page.getByText('abcd1234efgh5678')).toBeVisible();

    const url = new URL(page.url());
    expect(url.pathname).toBe(`/runs/${RUN_ID}`);
  });

  test('shows empty state when run has no sessions', async ({ page }) => {
    await setupCommonRoutes(page);

    await page.route((url) => new URL(url).pathname === '/api/sessions', async (route) => {
      await fulfillJson(route, {
        items: [],
        total: 0,
        limit: 50,
        offset: 0,
        next_cursor: null,
        prev_cursor: null,
      });
    });

    await page.goto(`/runs/${RUN_ID}`);
    await expect(page.getByRole('heading', { name: 'my-ci-run' })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText('No sessions yet for this run.')).toBeVisible();
  });

  test('shows error state and retry when sessions query fails', async ({ page }) => {
    await setupCommonRoutes(page);

    await page.route((url) => new URL(url).pathname === '/api/sessions', async (route) => {
      await route.fulfill({ status: 500, body: 'Internal Server Error' });
    });

    await page.goto(`/runs/${RUN_ID}`);

    // Run metadata still renders
    await expect(page.getByRole('heading', { name: 'my-ci-run' })).toBeVisible({ timeout: 15_000 });

    // Sessions error banner appears with retry button
    await expect(page.getByText('Could not load sessions for this run.')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible();
  });

  test('sessions query includes run_id param', async ({ page }) => {
    await setupCommonRoutes(page);

    let capturedRunId: string | null = null;
    await page.route((url) => new URL(url).pathname === '/api/sessions', async (route) => {
      capturedRunId = new URL(route.request().url()).searchParams.get('run_id');
      await fulfillJson(route, { items: [], total: 0, limit: 50, offset: 0, next_cursor: null, prev_cursor: null });
    });

    await page.goto(`/runs/${RUN_ID}`);
    await expect(page.getByRole('heading', { name: 'my-ci-run' })).toBeVisible({ timeout: 15_000 });

    // Wait for sessions panel to render (empty state)
    await expect(page.getByText('No sessions yet for this run.')).toBeVisible();
    expect(capturedRunId).toBe(RUN_ID);
  });

  test('shows escalated-to-maintenance badge when device cooldown threshold exceeded', async ({ page }) => {
    await mockEventsApi(page);
    await mockEmptySettingsApi(page);
    await page.route(`**/api/runs/${RUN_ID}`, async (route) => {
      if (route.request().method() !== 'GET') {
        await route.fallback();
        return;
      }
      await fulfillJson(route, MOCK_RUN_ESCALATED);
    });
    await page.route((url) => new URL(url).pathname === '/api/sessions', async (route) => {
      await fulfillJson(route, { items: [], total: 0, limit: 50, offset: 0, next_cursor: null, prev_cursor: null });
    });

    await page.goto(`/runs/${RUN_ID}`);
    await expect(page.getByRole('heading', { name: 'my-ci-run' })).toBeVisible({ timeout: 15_000 });

    // Escalated badge in the Reservation column
    await expect(page.getByText(/Escalated to maintenance/)).toBeVisible();
    // Cooldown count column shows the count (span with exact class)
    await expect(page.locator('span.text-text-2', { hasText: '3' }).first()).toBeVisible();
  });
});
