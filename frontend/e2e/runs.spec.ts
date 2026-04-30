import { test, expect } from './helpers/fixtures';

const RUNS = Array.from({ length: 55 }, (_, index) => ({
  id: `run-${index + 1}`,
  name: index === 0 ? 'z-run' : index === 1 ? 'a-run' : `run-${String(index + 1).padStart(3, '0')}`,
  state: 'preparing',
  requirements: [{ pack_id: 'appium-uiautomator2', platform_id: 'android_mobile', count: 1 }],
  ttl_minutes: 60,
  heartbeat_timeout_sec: 120,
  reserved_devices: [
    {
      device_id: `device-${index + 1}`,
      identity_value: `android-${index + 1}`,
      connection_target: `10.0.0.${index + 10}:5555`,
      pack_id: 'appium-uiautomator2',
      platform_id: 'android_mobile',
      platform_label: 'Android (real device)',
      os_version: '14',
      host_ip: '10.0.0.5',
      excluded: false,
      exclusion_reason: null,
      excluded_at: null,
    },
  ],
  error: null,
  created_at: new Date(Date.UTC(2026, 3, 4, 10 - index, 0, 0)).toISOString(),
  started_at: null,
  completed_at: null,
  created_by: 'github/actions',
  last_heartbeat: new Date(Date.UTC(2026, 3, 4, 10 - index, 0, 5)).toISOString(),
  session_counts: { passed: 0, failed: 0, error: 0, running: 0, total: 0 },
}));

test.describe('Runs page', () => {
  test.beforeEach(async ({ page }) => {
    await page.route((url) => new URL(url).pathname === '/api/events', async (route) => {
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
    });
    await page.route((url) => new URL(url).pathname === '/api/events/catalog', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ events: [{ name: 'run.created' }] }),
      });
    });
    await page.route('**/api/settings', async (route) => {
      if (route.request().method() !== 'GET') {
        await route.fallback();
        return;
      }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
    });
    await page.route((url) => new URL(url).pathname === '/api/runs', async (route) => {
      if (route.request().method() !== 'GET') {
        await route.fallback();
        return;
      }

      const urlObject = new URL(route.request().url());
      const limit = Number(urlObject.searchParams.get('limit') ?? '50');
      const cursor = urlObject.searchParams.get('cursor');
      const direction = urlObject.searchParams.get('direction') ?? 'older';

      const sorted = [...RUNS];
      let items = sorted.slice(0, limit);
      if (cursor) {
        const anchorIndex = sorted.findIndex((run) => run.id === cursor);
        if (anchorIndex >= 0 && direction === 'newer') {
          items = sorted.slice(Math.max(0, anchorIndex - limit), anchorIndex);
        } else if (anchorIndex >= 0) {
          items = sorted.slice(anchorIndex + 1, anchorIndex + 1 + limit);
        } else {
          items = [];
        }
      }
      const firstIndex = items.length > 0 ? sorted.findIndex((run) => run.id === items[0].id) : -1;
      const lastIndex = items.length > 0 ? sorted.findIndex((run) => run.id === items[items.length - 1].id) : -1;

      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items,
          limit,
          next_cursor: items.length > 0 && lastIndex < sorted.length - 1 ? items[items.length - 1].id : null,
          prev_cursor: items.length > 0 && firstIndex > 0 ? items[0].id : null,
        }),
      });
    });
  });

  test('shows cursor-based run history and restores state from the URL', async ({ page }) => {
    await page.goto('/runs');
    await expect(page.getByRole('heading', { name: 'Test Runs', exact: true })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText('Newest results first')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Name', exact: true })).toHaveCount(0);

    await page.getByRole('button', { name: 'Older' }).click();
    await expect(page).toHaveURL(/cursor=run-50/);
    await expect(page.getByText('Viewing historical results')).toBeVisible();

    await page.reload();

    await expect(page).toHaveURL(/cursor=run-50/);
    await expect(page.getByText('Viewing historical results')).toBeVisible();

    await page.getByRole('button', { name: 'Back to newest' }).click();
    await expect(page).not.toHaveURL(/cursor=/);
    await expect(page.getByText('Newest results first')).toBeVisible();
  });
});
