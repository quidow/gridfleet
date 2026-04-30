import { type Page, type Route } from '@playwright/test';
import { expect, test } from './helpers/fixtures';

type SettingType = 'int' | 'string' | 'bool' | 'json';

interface MockSetting {
  key: string;
  category: string;
  type: SettingType;
  description: string;
  default_value: unknown;
  value: unknown;
  validation: {
    min?: number;
    max?: number;
    allowed_values?: string[];
    item_type?: 'string';
    item_allowed_values?: string[];
  } | null;
}

const displayNames: Record<string, string> = {
  general: 'General',
  grid: 'Appium & Grid',
  notifications: 'Notifications',
  devices: 'Device Defaults',
  agent: 'Agent',
  reservations: 'Reservations',
  retention: 'Data Retention',
};

function createSettingsState(): MockSetting[] {
  return [
    {
      key: 'general.heartbeat_interval_sec',
      category: 'general',
      type: 'int',
      description: 'Heartbeat interval for hosts in seconds.',
      default_value: 15,
      value: 15,
      validation: { min: 5, max: 120 },
    },
    {
      key: 'grid.hub_url',
      category: 'grid',
      type: 'string',
      description: 'Base Selenium Grid hub URL.',
      default_value: 'http://selenium-hub:4444',
      value: 'http://selenium-hub:4444',
      validation: null,
    },
    {
      key: 'notifications.toast_events',
      category: 'notifications',
      type: 'json',
      description: 'Event names eligible for toast display.',
      default_value: ['node.crash', 'host.heartbeat_lost', 'device.availability_changed', 'run.expired'],
      value: ['node.crash', 'host.heartbeat_lost', 'device.availability_changed', 'run.expired'],
      validation: {
        item_type: 'string',
        item_allowed_values: [
          'device.availability_changed',
          'node.crash',
          'session.started',
          'run.created',
          'run.expired',
          'webhook.test',
        ],
      },
    },
    {
      key: 'notifications.toast_auto_dismiss_sec',
      category: 'notifications',
      type: 'int',
      description: 'Auto-dismiss delay for success toasts.',
      default_value: 5,
      value: 5,
      validation: { min: 0, max: 60 },
    },
    {
      key: 'devices.default_auto_manage',
      category: 'devices',
      type: 'bool',
      description: 'Default auto_manage value for newly discovered devices',
      default_value: true,
      value: true,
      validation: null,
    },
    {
      key: 'agent.min_version',
      category: 'agent',
      type: 'string',
      description: 'Minimum supported agent version.',
      default_value: '0.1.0',
      value: '0.1.0',
      validation: null,
    },
    {
      key: 'reservations.default_ttl_minutes',
      category: 'reservations',
      type: 'int',
      description: 'Default run TTL in minutes.',
      default_value: 60,
      value: 60,
      validation: { min: 1, max: 1440 },
    },
    {
      key: 'retention.sessions_days',
      category: 'retention',
      type: 'int',
      description: 'Days to retain completed sessions.',
      default_value: 30,
      value: 30,
      validation: { min: 1, max: 365 },
    },
  ];
}

function buildGroupedSettings(settings: MockSetting[]) {
  return Object.entries(displayNames).map(([category, display_name]) => ({
    category,
    display_name,
    settings: settings
      .filter((setting) => setting.category === category)
      .map((setting) => ({
        ...setting,
        is_overridden: JSON.stringify(setting.value) !== JSON.stringify(setting.default_value),
      })),
  }));
}

async function fulfillJson(route: Route, body: unknown) {
  await route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });
}

async function mockSettingsPageApis(page: Page) {
  const settings = createSettingsState();
  const hosts = [
    {
      id: 'host-1',
      hostname: 'lab-mac-mini',
      ip: '10.0.0.10',
      os_type: 'macos',
      agent_port: 5100,
      status: 'online',
      agent_version: '0.2.0',
      capabilities: null,
      last_heartbeat: '2026-03-30T10:00:00Z',
      created_at: '2026-03-30T10:00:00Z',
    },
  ];
  const webhooks = [
    {
      id: 'wh-1',
      name: 'Slack Alerts',
      url: 'https://hooks.slack.test/services/abc',
      event_types: ['device.availability_changed', 'session.started'],
      enabled: true,
      created_at: '2026-03-30T10:00:00Z',
      updated_at: '2026-03-30T10:00:00Z',
    },
  ];
  const webhookDeliveries: Record<string, { items: unknown[]; total: number }> = {
    'wh-1': {
      items: [
        {
          id: 'delivery-1',
          webhook_id: 'wh-1',
          event_type: 'device.availability_changed',
          status: 'exhausted',
          attempts: 3,
          max_attempts: 3,
          last_attempt_at: '2026-03-30T10:04:00Z',
          next_retry_at: null,
          last_error: '500 Internal Server Error',
          last_http_status: 500,
          created_at: '2026-03-30T10:00:10Z',
          updated_at: '2026-03-30T10:04:00Z',
        },
      ],
      total: 1,
    },
  };
  const eventCatalog = [
    {
      name: 'device.availability_changed',
      category: 'device_and_node_lifecycle',
      category_display_name: 'Device And Node Lifecycle',
      description: 'Device availability changed.',
      typical_data_fields: ['device_id'],
    },
    {
      name: 'node.crash',
      category: 'device_and_node_lifecycle',
      category_display_name: 'Device And Node Lifecycle',
      description: 'Managed Appium node crashed.',
      typical_data_fields: ['device_id'],
    },
    {
      name: 'session.started',
      category: 'sessions_and_runs',
      category_display_name: 'Sessions And Runs',
      description: 'Grid session started.',
      typical_data_fields: ['session_id'],
    },
    {
      name: 'run.created',
      category: 'sessions_and_runs',
      category_display_name: 'Sessions And Runs',
      description: 'Run created.',
      typical_data_fields: ['run_id'],
    },
    {
      name: 'run.expired',
      category: 'sessions_and_runs',
      category_display_name: 'Sessions And Runs',
      description: 'Run expired.',
      typical_data_fields: ['run_id'],
    },
    {
      name: 'webhook.test',
      category: 'operations_and_settings',
      category_display_name: 'Operations And Settings',
      description: 'Synthetic webhook test event.',
      typical_data_fields: ['webhook_id'],
    },
  ];

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
    await fulfillJson(route, buildGroupedSettings(settings));
  });

  await page.route((url) => new URL(url).pathname === '/api/events/catalog', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    await fulfillJson(route, { events: eventCatalog });
  });

  await page.route('**/api/settings/bulk', async (route) => {
    if (route.request().method() !== 'PUT') {
      await route.fallback();
      return;
    }

    const body = route.request().postDataJSON() as { settings: Record<string, unknown> };
    for (const [key, value] of Object.entries(body.settings)) {
      const setting = settings.find((entry) => entry.key === key);
      if (setting) {
        setting.value = value;
      }
    }

    const updated = settings
      .filter((setting) => keyInObject(setting.key, body.settings))
      .map((setting) => ({
        ...setting,
        is_overridden: JSON.stringify(setting.value) !== JSON.stringify(setting.default_value),
      }));

    await fulfillJson(route, updated);
  });

  await page.route('**/api/settings/reset-all', async (route) => {
    if (route.request().method() !== 'POST') {
      await route.fallback();
      return;
    }

    for (const setting of settings) {
      setting.value = setting.default_value;
    }

    await fulfillJson(route, { status: 'ok' });
  });

  await page.route('**/api/settings/reset/**', async (route) => {
    if (route.request().method() !== 'POST') {
      await route.fallback();
      return;
    }

    const key = decodeURIComponent(route.request().url().split('/reset/')[1] ?? '');
    const setting = settings.find((entry) => entry.key === key);
    if (!setting) {
      await route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ detail: 'Not found' }) });
      return;
    }

    setting.value = setting.default_value;
    await fulfillJson(route, {
      ...setting,
      is_overridden: false,
    });
  });

  await page.route('**/api/hosts/*/driver-packs', async (route) => {
    await fulfillJson(route, {
      host_id: 'host-1',
      packs: [{
        pack_id: 'appium-uiautomator2',
        pack_release: '2026.04.0',
        runtime_id: 'runtime-android',
        status: 'installed',
        resolved_install_spec: { appium_server: 'appium@2.11.5', appium_driver: { 'appium-uiautomator2-driver': '3.6.0' } },
        installer_log_excerpt: '',
        resolver_version: '1',
        blocked_reason: null,
        installed_at: '2026-04-26T00:00:00Z',
      }],
      runtimes: [{
        runtime_id: 'runtime-android',
        appium_server_package: 'appium',
        appium_server_version: '2.11.5',
        driver_specs: [{ package: 'appium-uiautomator2-driver', version: '3.6.0' }],
        plugin_specs: [],
        appium_home: '/tmp/appium/runtime-android',
        status: 'installed',
        blocked_reason: null,
      }],
      doctor: [],
    });
  });

  await page.route('**/api/hosts', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    await fulfillJson(route, hosts);
  });

  await page.route('**/api/webhooks', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    await fulfillJson(route, webhooks);
  });

  await page.route(/\/api\/webhooks\/[^/]+\/deliveries(\?.*)?$/, async (route) => {
    const request = route.request();
    if (request.method() !== 'GET') {
      await route.fallback();
      return;
    }
    const parts = new URL(request.url()).pathname.split('/');
    const webhookId = parts[3];
    await fulfillJson(route, webhookDeliveries[webhookId] ?? { items: [], total: 0 });
  });

  await page.route(/\/api\/webhooks\/[^/]+\/deliveries\/[^/]+\/retry$/, async (route) => {
    const request = route.request();
    if (request.method() !== 'POST') {
      await route.fallback();
      return;
    }
    const parts = new URL(request.url()).pathname.split('/');
    const webhookId = parts[3];
    const deliveryId = parts[5];
    const current = webhookDeliveries[webhookId]?.items.find(
      (item) => typeof item === 'object' && item !== null && (item as { id: string }).id === deliveryId,
    ) as Record<string, unknown> | undefined;
    const updated = {
      ...current,
      status: 'pending',
      attempts: 0,
      last_attempt_at: null,
      next_retry_at: '2026-03-30T10:05:00Z',
      last_error: null,
      last_http_status: null,
    };
    webhookDeliveries[webhookId] = { items: [updated], total: 1 };
    await fulfillJson(route, updated);
  });

}

function keyInObject(key: string, value: Record<string, unknown>): boolean {
  return Object.prototype.hasOwnProperty.call(value, key);
}

test.describe('Settings Page', () => {
  test.beforeEach(async ({ page }) => {
    await mockSettingsPageApis(page);
  });

  test('loads with all 10 tabs visible in grouped tab strip', async ({ page }) => {
    await page.goto('/settings');

    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible();

    for (const label of [
      'General',
      'Appium & Grid',
      'Notifications',
      'Device Defaults',
      'Agent',
      'Reservations',
      'Data Retention',
      'Appium Plugins',
      'Drivers',
      'Webhooks',
    ]) {
      await expect(page.getByRole('button', { name: label })).toBeVisible();
    }
  });

  test('Reset All Settings is a danger button that opens confirmation and cancels safely', async ({ page }) => {
    await page.goto('/settings');
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible();

    // Should be a visible danger-styled button (not a plain link)
    const resetBtn = page.getByRole('button', { name: 'Reset All Settings' });
    await expect(resetBtn).toBeVisible();

    // Click opens a confirmation dialog
    await resetBtn.click();
    await expect(page.getByRole('dialog')).toBeVisible();
    await expect(page.getByText('This will reset all settings to their default values.')).toBeVisible();

    // Cancelling closes the dialog without mutating
    await page.getByRole('button', { name: 'Cancel' }).click();
    await expect(page.getByRole('dialog')).not.toBeVisible();
  });

  test('each tab shows its expected fields and management views', async ({ page }) => {
    await page.goto('/settings');

    await expect(page.locator('input[name="general.heartbeat_interval_sec"]')).toBeVisible();

    await page.getByRole('button', { name: 'Appium & Grid' }).click();
    await expect(page.locator('input[name="grid.hub_url"]')).toBeVisible();

    await page.getByRole('button', { name: 'Notifications' }).click();
    await expect(page.locator('input[name="notifications.toast_auto_dismiss_sec"]')).toBeVisible();
    await expect(page.getByLabel('run.expired')).toBeVisible();
    await expect(page.getByLabel('device.health_changed')).toHaveCount(0);

    await page.getByRole('button', { name: 'Device Defaults' }).click();
    await expect(page.getByText('Default auto_manage value for newly discovered devices')).toBeVisible();

    await page.getByRole('button', { name: 'Agent' }).click();
    await expect(page.locator('input[name="agent.min_version"]')).toBeVisible();

    await page.getByRole('button', { name: 'Reservations' }).click();
    await expect(page.locator('input[name="reservations.default_ttl_minutes"]')).toBeVisible();

    await page.getByRole('button', { name: 'Data Retention' }).click();
    await expect(page.locator('input[name="retention.sessions_days"]')).toBeVisible();

    await page.getByRole('button', { name: 'Drivers' }).click();
    await expect(page.getByText('Driver packs are now managed from their own section.')).toBeVisible();
    await expect(page.getByRole('link', { name: 'View All Driver Packs' })).toHaveAttribute('href', '/drivers');

    await page.getByRole('button', { name: 'Webhooks' }).click();
    await expect(page.getByRole('button', { name: 'Add Webhook' })).toBeVisible();
    await expect(page.getByRole('table')).toBeVisible();
    await page.getByRole('button', { name: 'Add Webhook' }).click();
    await expect(page.getByLabel('run.created')).toBeVisible();
    await expect(page.getByLabel('webhook.test')).toBeVisible();
    await expect(page.getByLabel('device.health_changed')).toHaveCount(0);
  });

  test('webhook recent deliveries are visible and retryable', async ({ page }) => {
    await page.goto('/settings');

    await page.getByRole('button', { name: 'Webhooks' }).click();
    await page.getByRole('button', { name: 'Recent Deliveries' }).click();

    await expect(page.getByText('device.availability_changed').nth(1)).toBeVisible();
    await expect(page.getByText('500 Internal Server Error')).toBeVisible();

    await page.getByRole('button', { name: 'Retry' }).click();

    await expect(page.getByText('pending')).toBeVisible();
    await expect(page.getByText('Attempt 0 of 3')).toBeVisible();
  });

  test('settings fields are grouped into labeled cards', async ({ page }) => {
    await page.goto('/settings');

    await expect(page.getByRole('heading', { name: 'Heartbeat & Host Health' })).toBeVisible();

    await page.getByRole('button', { name: 'Appium & Grid' }).click();
    await expect(page.getByRole('heading', { name: 'Grid Routing' })).toBeVisible();

    await page.getByRole('button', { name: 'Notifications' }).click();
    await expect(page.getByRole('heading', { name: 'Toast Events' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Toast Delivery' })).toBeVisible();
  });

  test('changing and saving a setting persists after reload', async ({ page }) => {
    await page.goto('/settings');

    const heartbeatInput = page.locator('input[name="general.heartbeat_interval_sec"]');
    await expect(heartbeatInput).toHaveValue('15');

    await heartbeatInput.fill('20');
    await page.getByRole('button', { name: 'Save Changes' }).click();

    await expect(page.getByText('Settings saved')).toBeVisible();
    await expect(page.getByText('Modified')).toBeVisible();

    await page.reload();

    await expect(page.locator('input[name="general.heartbeat_interval_sec"]')).toHaveValue('20');
    await expect(page.getByText('Modified')).toBeVisible();
  });

  test('reset to default restores the original value', async ({ page }) => {
    await page.goto('/settings');

    const heartbeatInput = page.locator('input[name="general.heartbeat_interval_sec"]');
    await heartbeatInput.fill('22');
    await page.getByRole('button', { name: 'Save Changes' }).click();
    await expect(page.getByText('Modified')).toBeVisible();

    const heartbeatField = page.locator('div').filter({ has: page.getByText('Heartbeat Interval Sec') }).first();
    await heartbeatField.getByTitle('Reset to default').click();

    await expect(page.getByText('Setting reset to default')).toBeVisible();
    await expect(page.locator('input[name="general.heartbeat_interval_sec"]')).toHaveValue('15');
    await expect(page.getByText('Modified')).toHaveCount(0);
  });

  test('settings tab is URL-addressable and restores on reload', async ({ page }) => {
    await page.goto('/settings?tab=driver-packs');
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText('Driver packs are now managed from their own section.')).toBeVisible();

    // Switch to Webhooks and URL should update
    await page.getByRole('button', { name: 'Webhooks', exact: true }).click();
    await expect(page).toHaveURL(/tab=webhooks/);
    await expect(page.getByRole('heading', { name: 'Webhooks' })).toBeVisible();
  });
});
