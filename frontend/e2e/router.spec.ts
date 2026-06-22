import { test, expect } from './helpers/fixtures';
import { mockAppShellApis } from './helpers/routes';

const ROUTER_FIXTURE = {
  counts: {
    registered: 2,
    running: 2,
    available: 1,
    busy: 1,
    verifying: 0,
    offline: 0,
    maintenance: 0,
    eligible: 1,
    active_sessions: 1,
    queue_depth: 1,
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
      session_id: null,
      session_target: null,
      stereotype: { platformName: 'Android', 'gridfleet:deviceId': '7f3a' },
    },
    {
      device_id: 'd2',
      device_name: 'iPhone 15',
      platform_id: 'ios',
      host_id: 'h2',
      host_name: 'host-b',
      operational_state: 'busy',
      node_effective_state: 'running',
      session_id: 's_4821',
      session_target: 'http://host-b:8100',
      stereotype: { platformName: 'iOS', 'gridfleet:deviceId': '2b9c' },
    },
  ],
  queue: [
    {
      requestId: 'q1',
      capabilities: { platformName: 'Android', 'gridfleet:tag:team': 'qa' },
      requestTimestamp: new Date().toISOString(),
      runId: null,
    },
  ],
};

test.describe('Router page', () => {
  test.beforeEach(async ({ page, context }) => {
    await mockAppShellApis(page);
    await page.route('**/api/grid/router', (route) => route.fulfill({ json: ROUTER_FIXTURE }));
    await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  });

  test('renders node cards, counts, queue, and the session route', async ({ page }) => {
    await page.goto('/router');
    await expect(page.getByRole('heading', { name: 'Router' })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText('Pixel 7')).toBeVisible();
    await expect(page.getByText('iPhone 15')).toBeVisible();
    await expect(page.getByText('open', { exact: true })).toBeVisible();
    // queue side-panel shows the one waiting, run-less ticket
    await expect(page.getByText('Queue (1)')).toBeVisible();
    await expect(page.getByText('free')).toBeVisible();
    // the busy node surfaces its session id + routing target
    await expect(page.getByText(/s_4821/)).toBeVisible();
  });

  test('search filters the node wall by device name', async ({ page }) => {
    await page.goto('/router');
    await expect(page.getByText('Pixel 7')).toBeVisible({ timeout: 15_000 });
    await page.getByRole('textbox', { name: 'Search device' }).fill('iPhone');
    await expect(page.getByText('Pixel 7')).toHaveCount(0);
    await expect(page.getByText('iPhone 15')).toBeVisible();
  });

  test('copy keys confirms with a "Copied" state', async ({ page }) => {
    await page.goto('/router');
    await expect(page.getByText('Pixel 7')).toBeVisible({ timeout: 15_000 });
    await page.getByRole('button', { name: 'Copy keys' }).first().click();
    await expect(page.getByText('Copied').first()).toBeVisible();
  });
});
