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
  grid: 'Appium & Allocation',
  notifications: 'Notifications',
  device_checks: 'Device Checks',
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
      key: 'grid.session_poll_interval_sec',
      category: 'grid',
      type: 'int',
      description: 'Interval of the direct-to-Appium session observation sweep.',
      default_value: 30,
      value: 30,
      validation: { min: 1, max: 300 },
    },
    {
      key: 'notifications.toast_events',
      category: 'notifications',
      type: 'json',
      description: 'Event names eligible for toast display.',
      default_value: [
        'node.crash',
        'host.heartbeat_lost',
        'device.operational_state_changed',
        'run.expired',
      ],
      value: [
        'node.crash',
        'host.heartbeat_lost',
        'device.operational_state_changed',
        'run.expired',
      ],
      validation: {
        item_type: 'string',
        item_allowed_values: [
          'device.operational_state_changed',
          'node.crash',
          'session.started',
          'run.created',
          'run.expired',
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
    {
      key: 'device_checks.ip_ping.consecutive_fail_threshold',
      category: 'device_checks',
      type: 'int',
      description: 'Consecutive ICMP-ping misses before a device is marked unhealthy.',
      default_value: 3,
      value: 3,
      validation: { min: 1, max: 50 },
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
  const eventCatalog = [
    {
      name: 'device.operational_state_changed',
      category: 'device_and_node_lifecycle',
      category_display_name: 'Device And Node Lifecycle',
      description: 'Device operational state changed.',
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

}

function keyInObject(key: string, value: Record<string, unknown>): boolean {
  return Object.prototype.hasOwnProperty.call(value, key);
}

test.describe('Settings Page', () => {
  test.beforeEach(async ({ page }) => {
    await mockSettingsPageApis(page);
  });

  test('loads with all 9 tabs visible in grouped tab strip', async ({ page }) => {
    await page.goto('/settings');

    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible();

    for (const label of [
      'General',
      'Appium & Allocation',
      'Agent',
      'Device Checks',
      'Reservations',
      'Data Retention',
      'Backup & Restore',
      'Notifications',
      'Appium Plugins',
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

    await page.getByRole('button', { name: 'Appium & Allocation' }).click();
    await expect(page.locator('input[name="grid.session_poll_interval_sec"]')).toBeVisible();

    await page.getByRole('button', { name: 'Notifications' }).click();
    await expect(page.locator('input[name="notifications.toast_auto_dismiss_sec"]')).toBeVisible();
    await expect(page.getByLabel('run.expired')).toBeVisible();
    await expect(page.getByLabel('device.health_changed')).toHaveCount(0);

    await page.getByRole('button', { name: 'Agent' }).click();
    await expect(page.locator('input[name="agent.min_version"]')).toBeVisible();

    await page.getByRole('button', { name: 'Reservations' }).click();
    await expect(page.locator('input[name="reservations.default_ttl_minutes"]')).toBeVisible();

    await page.getByRole('button', { name: 'Data Retention' }).click();
    await expect(page.locator('input[name="retention.sessions_days"]')).toBeVisible();

    await page.getByRole('button', { name: 'Backup & Restore' }).click();
    await expect(page.getByText('Export configuration')).toBeVisible();
  });

  test('settings fields are grouped into labeled cards', async ({ page }) => {
    await page.goto('/settings');

    await expect(page.getByRole('heading', { name: 'Heartbeat & Host Health' })).toBeVisible();

    await page.getByRole('button', { name: 'Appium & Allocation' }).click();
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
    await page.goto('/settings?tab=backup');
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText('Export configuration')).toBeVisible();

    // Switch to Notifications and URL should update
    await page.getByRole('button', { name: 'Notifications', exact: true }).click();
    await expect(page).toHaveURL(/tab=notifications/);
    await expect(page.getByRole('heading', { name: 'Toast Events' })).toBeVisible();
  });
});
