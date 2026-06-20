import { test, expect } from './helpers/fixtures';
import { mockAppShellApis } from './helpers/routes';

const ROUTER_FIXTURE = {
  counts: {
    registered: 1,
    running: 1,
    available: 1,
    busy: 0,
    verifying: 0,
    offline: 0,
    maintenance: 0,
    active_sessions: 0,
    queue_depth: 0,
  },
  nodes: [
    {
      device_id: 'd1',
      device_name: 'Pixel 7',
      platform_id: 'android_mobile',
      host_id: 'h1',
      host_name: 'host-a',
      operational_state: 'available',
      node_effective_state: 'running',
      node_port: 4723,
      connection_target: 'emulator-5554',
      session_id: null,
      session_target: null,
      stereotype: { platformName: 'Android', 'appium:gridfleet:deviceId': '7f3a' },
    },
  ],
  queue: [],
};

test.describe('Router page', () => {
  test.beforeEach(async ({ page }) => {
    await mockAppShellApis(page);
    await page.route('**/api/grid/router', (route) => route.fulfill({ json: ROUTER_FIXTURE }));
  });

  test('shows nodes and counts', async ({ page }) => {
    await page.goto('/router');
    await expect(page.getByRole('heading', { name: 'Router' })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText('Pixel 7')).toBeVisible();
    await expect(page.getByText('Registered')).toBeVisible();
  });
});
