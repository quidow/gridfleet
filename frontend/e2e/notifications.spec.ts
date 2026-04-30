import { type Page, type Route } from '@playwright/test';
import { expect, test } from './helpers/fixtures';

async function fulfillJson(route: Route, body: unknown) {
  await route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });
}

const EVENTS = Array.from({ length: 60 }, (_, index) => ({
  id: `evt-${index + 1}`,
  type: index < 30 ? 'run.created' : 'device.availability_changed',
  timestamp: new Date(Date.UTC(2026, 3, 1, 10, 0 - index, 0)).toISOString(),
  data: index < 30 ? { run_id: `run-${index + 1}`, name: `Smoke Run ${index + 1}` } : { device_id: `dev-${index + 1}` },
}));

async function mockNotificationsApis(page: Page) {
  await page.route((url) => new URL(url).pathname === '/api/events', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: '',
    });
  });

  await page.route('**/api/settings', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    await fulfillJson(route, []);
  });

  await page.route((url) => new URL(url).pathname === '/api/events/catalog', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    await fulfillJson(route, {
      events: [
        {
          name: 'device.availability_changed',
          category: 'device_and_node_lifecycle',
          category_display_name: 'Device And Node Lifecycle',
          description: 'Device availability changed.',
          typical_data_fields: ['device_id'],
        },
        {
          name: 'run.created',
          category: 'sessions_and_runs',
          category_display_name: 'Sessions And Runs',
          description: 'Run created.',
          typical_data_fields: ['run_id'],
        },
      ],
    });
  });

  await page.route((url) => new URL(url).pathname === '/api/notifications', async (route) => {
    const urlObject = new URL(route.request().url());
    const limit = Number(urlObject.searchParams.get('limit') ?? '25');
    const offset = Number(urlObject.searchParams.get('offset') ?? '0');
    const typeFilter = urlObject.searchParams.get('types');
    const filtered = typeFilter ? EVENTS.filter((event) => event.type === typeFilter) : EVENTS;

    await fulfillJson(route, {
      items: filtered.slice(offset, offset + limit),
      total: filtered.length,
      limit,
      offset,
    });
  });
}

test.describe('Notifications Page', () => {
  test.beforeEach(async ({ page }) => {
    await mockNotificationsApis(page);
  });

  test('uses paginated notifications and restores filtered page state from the URL', async ({ page }) => {
    await page.goto('/notifications');

    await expect(page.getByRole('heading', { name: 'Notifications', exact: true })).toBeVisible();
    await expect(page.locator('option[value="run.created"]')).toHaveCount(1);
    await expect(page.getByText('Showing 1-25 of 60')).toBeVisible();

    await page.selectOption('select', 'run.created');
    await expect(page).toHaveURL(/type=run\.created/);
    await expect(page.getByText('Showing 1-25 of 30')).toBeVisible();

    await page.getByRole('button', { name: 'Next' }).click();
    await expect(page).toHaveURL(/page=2/);
    await expect(page.getByText('Showing 26-30 of 30')).toBeVisible();
    await expect(page.getByText('Smoke Run 26')).toBeVisible();

    await page.reload();

    await expect(page).toHaveURL(/type=run\.created/);
    await expect(page).toHaveURL(/page=2/);
    await expect(page.getByText('Showing 26-30 of 30')).toBeVisible();
    await expect(page.getByText('Smoke Run 26')).toBeVisible();
  });
});
