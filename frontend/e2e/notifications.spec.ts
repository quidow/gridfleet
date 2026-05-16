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
  type: index < 30 ? 'run.created' : 'device.operational_state_changed',
  timestamp: new Date(Date.UTC(2026, 3, 1, 10, 0 - index, 0)).toISOString(),
  data: index < 30 ? { run_id: `run-${index + 1}`, name: `Smoke Run ${index + 1}` } : { device_id: `dev-${index + 1}` },
}));

// Four representative events covering each severity level driven by the backend field.
const SEVERITY_EVENTS = [
  {
    id: 'evt-probe',
    type: 'device.operational_state_changed',
    severity: 'info',
    timestamp: new Date(Date.UTC(2026, 4, 1, 9, 0, 0)).toISOString(),
    data: { device_id: 'dev-probe', reason: 'Session viability probe running' },
  },
  {
    id: 'evt-recover',
    type: 'device.operational_state_changed',
    severity: 'success',
    timestamp: new Date(Date.UTC(2026, 4, 1, 9, 1, 0)).toISOString(),
    data: { device_id: 'dev-recover', reason: 'Health checks recovered' },
  },
  {
    id: 'evt-crash',
    type: 'node.crash',
    severity: 'critical',
    timestamp: new Date(Date.UTC(2026, 4, 1, 9, 2, 0)).toISOString(),
    data: { device_name: 'test-device' },
  },
  {
    id: 'evt-settings',
    type: 'settings.changed',
    severity: 'neutral',
    timestamp: new Date(Date.UTC(2026, 4, 1, 9, 3, 0)).toISOString(),
    data: { key: 'heartbeat_interval_sec' },
  },
];

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
          name: 'device.operational_state_changed',
          category: 'device_and_node_lifecycle',
          category_display_name: 'Device And Node Lifecycle',
          description: 'Device operational state changed.',
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

async function mockSeverityNotificationsApis(page: Page) {
  await page.route((url) => new URL(url).pathname === '/api/events', async (route) => {
    await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
  });

  await page.route('**/api/settings', async (route) => {
    if (route.request().method() !== 'GET') { await route.fallback(); return; }
    await fulfillJson(route, []);
  });

  await page.route((url) => new URL(url).pathname === '/api/events/catalog', async (route) => {
    if (route.request().method() !== 'GET') { await route.fallback(); return; }
    await fulfillJson(route, {
      events: [
        { name: 'device.operational_state_changed', category: 'device_and_node_lifecycle', category_display_name: 'Device And Node Lifecycle', description: '', typical_data_fields: [] },
        { name: 'node.crash', category: 'device_and_node_lifecycle', category_display_name: 'Device And Node Lifecycle', description: '', typical_data_fields: [] },
        { name: 'settings.changed', category: 'settings', category_display_name: 'Settings', description: '', typical_data_fields: [] },
      ],
    });
  });

  await page.route((url) => new URL(url).pathname === '/api/notifications', async (route) => {
    const urlObject = new URL(route.request().url());
    const limit = Number(urlObject.searchParams.get('limit') ?? '25');
    const offset = Number(urlObject.searchParams.get('offset') ?? '0');
    await fulfillJson(route, {
      items: SEVERITY_EVENTS.slice(offset, offset + limit),
      total: SEVERITY_EVENTS.length,
      limit,
      offset,
    });
  });
}

test.describe('Notifications Page — severity badges', () => {
  test.beforeEach(async ({ page }) => {
    await mockSeverityNotificationsApis(page);
  });

  test('renders backend-driven severity badges for each event row', async ({ page }) => {
    await page.goto('/notifications');
    await expect(page.getByRole('heading', { name: 'Notifications', exact: true })).toBeVisible();

    // Each assertion finds the table row that contains the given event type code
    // and then checks that the severity badge label is present within that same row.

    const table = page.locator('table');

    // evt-probe: device.operational_state_changed with severity=info => badge "Info"
    const probeRow = table.locator('tr', { has: page.locator('code', { hasText: 'device.operational_state_changed' }).first() }).first();
    await expect(probeRow.getByText('Info')).toBeVisible();

    // evt-recover: same event type but severity=success — the second row with that type => "Success"
    const recoverRow = table.locator('tr', { has: page.locator('code', { hasText: 'device.operational_state_changed' }) }).nth(1);
    await expect(recoverRow.getByText('Success')).toBeVisible();

    // evt-crash: node.crash with severity=critical => badge "Critical"
    const crashRow = table.locator('tr', { has: page.locator('code', { hasText: 'node.crash' }) });
    await expect(crashRow.getByText('Critical')).toBeVisible();

    // evt-settings: settings.changed with severity=neutral => badge "Neutral"
    const settingsRow = table.locator('tr', { has: page.locator('code', { hasText: 'settings.changed' }) });
    await expect(settingsRow.getByText('Neutral')).toBeVisible();
  });
});

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
