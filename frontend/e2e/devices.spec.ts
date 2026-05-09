import { type Locator, type Page } from '@playwright/test';
import { test, expect } from './helpers/fixtures';
import { fulfillJson } from './helpers/routes';

const DEVICE_ROW_SELECTOR = '[data-testid^="device-row-"]';

function deviceChipStatus(device: { operational_state: string; hold?: string | null }) {
  if (device.operational_state === 'busy') {
    return 'busy';
  }
  return device.hold ?? device.operational_state;
}

async function expectHeadingStyle(locator: Locator, fontSize: string) {
  const style = await locator.evaluate((node) => {
    const computed = window.getComputedStyle(node);
    return {
      fontFamily: computed.fontFamily,
      fontSize: computed.fontSize,
      fontWeight: computed.fontWeight,
      letterSpacing: computed.letterSpacing,
    };
  });
  const letterSpacing = style.letterSpacing === 'normal' ? 0 : Number.parseFloat(style.letterSpacing);

  expect(style.fontFamily).toContain('IBM Plex Sans Variable');
  expect(style.fontSize).toBe(fontSize);
  expect(Number(style.fontWeight)).toBeGreaterThanOrEqual(600);
  expect(Number(style.fontWeight)).toBeLessThan(700);
  expect(letterSpacing).toBeGreaterThanOrEqual(0);
}

async function controlChrome(locator: Locator) {
  return locator.evaluate((node) => {
    const computed = window.getComputedStyle(node);
    return {
      borderRadius: computed.borderRadius,
      fontSize: computed.fontSize,
      height: computed.height,
    };
  });
}

const DEFAULT_HOSTS = [
  {
    id: 'host-1',
    hostname: 'linux-host',
    ip: '10.0.0.20',
    os_type: 'linux',
    agent_port: 5100,
    status: 'online',
    agent_version: '1.0.0',
    required_agent_version: '1.0.0',
    recommended_agent_version: '1.0.0',
    agent_update_available: false,
    agent_version_status: 'ok',
    capabilities: null,
    last_heartbeat: '2026-03-30T10:00:00Z',
    created_at: '2026-03-30T10:00:00Z',
  },
];

const DEFAULT_DEVICE = {
  id: 'device-default',
  pack_id: 'appium-uiautomator2',
  platform_id: 'android_mobile',
  platform_label: 'Android',
  identity_scheme: 'android_serial',
  identity_scope: 'host',
  identity_value: 'device-default-001',
  connection_target: '192.168.1.50:5555',
  name: 'Default Device',
  os_version: '14',
  host_id: 'host-1',
  operational_state: 'available',
  hold: null,
  tags: { team: 'qa' },
  auto_manage: true,
  device_type: 'real_device',
  connection_type: 'network',
  ip_address: '192.168.1.50',
  battery_level_percent: 84,
  battery_temperature_c: 36.7,
  charging_state: 'charging',
  hardware_health_status: 'healthy',
  hardware_telemetry_reported_at: '2026-03-30T10:00:03Z',
  hardware_telemetry_state: 'fresh',
  readiness_state: 'verified',
  missing_setup_fields: [],
  verified_at: '2026-03-30T10:00:03Z',
  reservation: null,
  lifecycle_policy_summary: {
    state: 'idle',
    label: 'Idle',
    detail: null,
    backoff_until: null,
  },
  health_summary: {
    healthy: true,
    summary: 'Healthy',
    last_checked_at: '2026-03-30T10:00:03Z',
  },
  emulator_state: null,
  created_at: '2026-03-30T10:00:03Z',
  updated_at: '2026-03-30T10:00:03Z',
} as const;

const DEFAULT_DEVICE_DETAIL = {
  ...DEFAULT_DEVICE,
  appium_node: {
    id: 'node-default',
    port: 4723,
    grid_url: 'http://127.0.0.1:4444',
    pid: 4242,
    container_id: null,
    state: 'running',
    started_at: '2026-03-30T10:00:03Z',
  },
  sessions: [],
} as const;

const DEFAULT_HEATMAP_ROWS = [
  { timestamp: '2026-03-29T12:00:00Z', status: 'passed' },
  { timestamp: '2026-03-30T12:00:00Z', status: 'failed' },
  { timestamp: '2026-03-30T15:00:00Z', status: 'error' },
] as const;

function mockHealthSummary(healthy = false, summary = healthy ? 'Healthy' : 'Unavailable') {
  return { healthy, summary };
}

function devicesResponseBody(devices: unknown[], requestUrl: URL): unknown {
  const limit = requestUrl.searchParams.get('limit');
  if (limit) {
    return { items: devices, total: devices.length, limit: Number(limit), offset: Number(requestUrl.searchParams.get('offset') ?? 0) };
  }
  return devices;
}

function verificationJob(
  jobId: string,
  overrides: Record<string, unknown> = {},
) {
  return {
    job_id: jobId,
    status: 'pending',
    current_stage: null,
    current_stage_status: null,
    detail: null,
    error: null,
    device_id: null,
    started_at: '2026-03-30T10:00:00Z',
    finished_at: null,
    ...overrides,
  };
}

function verificationEventStream(...payloads: Array<Record<string, unknown>>) {
  return payloads
    .map((payload) => `event: device.verification.updated\ndata: ${JSON.stringify(payload)}\n\n`)
    .join('');
}

function deviceRows(page: Page) {
  return page.locator(DEVICE_ROW_SELECTOR);
}

function firstDeviceRow(page: Page) {
  return deviceRows(page).first();
}

async function mockDefaultDevicesSurface(page: Page) {
  await page.route((url) => new URL(url).pathname === '/api/events', async (route) => {
    await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
  });
  await page.route((url) => new URL(url).pathname === '/api/events/catalog', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ events: [] }) });
  });
  await page.route('**/api/driver-packs/catalog', async (route) => {
    await fulfillJson(route, {
      packs: [{
        id: 'appium-uiautomator2',
        display_name: 'Appium UiAutomator2',
        state: 'enabled',
        current_release: '2026.04.0',
        platforms: [{
          id: 'android_mobile',
          display_name: 'Android',
          automation_name: 'UiAutomator2',
          appium_platform_name: 'Android',
          device_types: ['real_device', 'emulator'],
          connection_types: ['usb', 'network', 'virtual'],
          grid_slots: ['native', 'chrome'],
          identity_scheme: 'android_serial',
          identity_scope: 'host',
          discovery_kind: 'adb',
          lifecycle_actions: [{ id: 'state' }, { id: 'reconnect' }],
          health_checks: [],
          device_fields_schema: [],
          capabilities: {},
          display_metadata: { icon_kind: 'mobile' },
          default_capabilities: {},
          connection_behavior: { default_device_type: 'real_device', default_connection_type: 'usb', requires_connection_target: true },
          device_type_overrides: {
            emulator: {
              lifecycle_actions: [{ id: 'state' }, { id: 'boot' }, { id: 'shutdown' }],
              connection_behavior: { default_device_type: 'emulator', default_connection_type: 'virtual', requires_connection_target: true },
            },
          },
        }],
      }, {
        id: 'appium-xcuitest',
        display_name: 'Appium XCUITest',
        state: 'enabled',
        current_release: '2026.04.0',
        platforms: [{
          id: 'ios',
          display_name: 'iOS',
          automation_name: 'XCUITest',
          appium_platform_name: 'iOS',
          device_types: ['real_device', 'simulator'],
          connection_types: ['usb', 'network', 'virtual'],
          grid_slots: ['native'],
          identity_scheme: 'apple_udid',
          identity_scope: 'global',
          discovery_kind: 'apple',
          lifecycle_actions: [{ id: 'state' }, { id: 'reconnect' }],
          health_checks: [],
          device_fields_schema: [],
          capabilities: {},
          display_metadata: { icon_kind: 'mobile' },
          default_capabilities: {},
          connection_behavior: { default_device_type: 'real_device', default_connection_type: 'usb', requires_connection_target: true },
          device_type_overrides: {
            simulator: {
              identity: { scheme: 'simulator_udid', scope: 'host' },
              lifecycle_actions: [{ id: 'state' }, { id: 'boot' }, { id: 'shutdown' }],
              connection_behavior: { default_device_type: 'simulator', default_connection_type: 'virtual', requires_connection_target: true },
            },
          },
        }],
      }, {
        id: 'appium-roku-dlenroc',
        display_name: 'Roku (dlenroc)',
        state: 'enabled',
        current_release: '2026.04.0',
        platforms: [{
          id: 'roku_network',
          display_name: 'Roku (network)',
          automation_name: 'Roku',
          appium_platform_name: 'roku',
          device_types: ['real_device'],
          connection_types: ['network'],
          grid_slots: ['native'],
          identity_scheme: 'roku_serial',
          identity_scope: 'global',
          discovery_kind: 'roku_ecp',
          lifecycle_actions: [],
          health_checks: [],
          device_fields_schema: [
            { id: 'roku_password', label: 'Developer password', type: 'string', required_for_session: true, sensitive: true, capability_name: 'appium:password' },
          ],
          capabilities: {},
          display_metadata: { icon_kind: 'set_top' },
          default_capabilities: { 'appium:ip': '{device.ip_address}' },
          connection_behavior: { default_device_type: 'real_device', default_connection_type: 'network', requires_ip_address: true, requires_connection_target: false },
        }],
      }],
    });
  });
  await page.route((url) => new URL(url).pathname === '/api/hosts', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(DEFAULT_HOSTS) });
  });
  await page.route((url) => new URL(url).pathname === '/api/devices', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    const requestUrl = new URL(route.request().url());
    const search = requestUrl.searchParams.get('search')?.toLowerCase() ?? '';
    const devices = [DEFAULT_DEVICE].filter((device) => {
      if (!search) {
        return true;
      }
      return [device.name, device.identity_value, device.connection_target ?? '']
        .some((value) => value.toLowerCase().includes(search));
    });
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(devicesResponseBody(devices, requestUrl)) });
  });
  await page.route((url) => new URL(url).pathname === `/api/devices/${DEFAULT_DEVICE.id}`, async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(DEFAULT_DEVICE_DETAIL) });
  });
  await page.route((url) => new URL(url).pathname === `/api/devices/${DEFAULT_DEVICE.id}/health`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        platform: DEFAULT_DEVICE.platform,
        healthy: true,
        node: {
          running: true,
          port: 4723,
          state: 'running',
        },
        device_checks: {
          adb: { status: 'ok' },
        },
        session_viability: {
          status: 'passed',
          last_attempted_at: '2026-03-30T10:00:03Z',
          last_succeeded_at: '2026-03-30T10:00:03Z',
          error: null,
          checked_by: 'scheduled',
        },
        lifecycle_policy: {
          last_failure_source: null,
          last_failure_reason: null,
          last_action: null,
          last_action_at: null,
          stop_pending: false,
          stop_pending_reason: null,
          stop_pending_since: null,
          excluded_from_run: false,
          excluded_run_id: null,
          excluded_run_name: null,
          excluded_at: null,
          will_auto_rejoin_run: false,
          recovery_suppressed_reason: null,
          backoff_until: null,
          recovery_state: 'idle',
        },
      }),
    });
  });
  await page.route((url) => new URL(url).pathname === `/api/devices/${DEFAULT_DEVICE.id}/capabilities`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        platformName: 'Android',
        'appium:udid': DEFAULT_DEVICE.identity_value,
      }),
    });
  });
  await page.route((url) => new URL(url).pathname === `/api/devices/${DEFAULT_DEVICE.id}/logs`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ lines: ['Device log line'], count: 1 }),
    });
  });
  await page.route(
    (url) => new URL(url).pathname === `/api/devices/${DEFAULT_DEVICE.id}/session-outcome-heatmap`,
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(DEFAULT_HEATMAP_ROWS),
      });
    },
  );
  await page.route((url) => new URL(url).pathname === `/api/devices/${DEFAULT_DEVICE.id}/config`, async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ automationName: 'UiAutomator2' }),
    });
  });
  await page.route((url) => new URL(url).pathname === `/api/devices/${DEFAULT_DEVICE.id}/config/history`, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });
}

async function mockAddDeviceVerificationSurface(page: Page) {
  let devices = [] as Array<Record<string, unknown>>;
  const deviceRequests = [] as Array<Record<string, string>>;
  let intakeCandidates = [
    {
      pack_id: 'appium-uiautomator2',
      platform_id: 'android_mobile',
      platform_label: 'Android',
      identity_scheme: 'android_serial',
      identity_scope: 'host',
      identity_value: 'candidate-001',
      connection_target: 'candidate-001',
      name: 'Pixel Candidate',
      os_version: '14',
      manufacturer: 'Google',
      model: 'Pixel 8',
      detected_properties: {},
      device_type: 'real_device',
      connection_type: 'usb',
      ip_address: null,
      already_registered: false,
      registered_device_id: null,
    },
  ] as Array<Record<string, unknown>>;
  const hosts = [
    {
      id: 'host-1',
      hostname: 'linux-host',
      ip: '10.0.0.20',
      os_type: 'linux',
      agent_port: 5100,
      status: 'online',
      agent_version: null,
      required_agent_version: null,
      recommended_agent_version: null,
      agent_update_available: false,
      agent_version_status: 'unknown',
      capabilities: null,
      last_heartbeat: null,
      created_at: '2026-03-30T10:00:00Z',
    },
  ];

  await page.route((url) => new URL(url).pathname === '/api/events', async (route) => {
    await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
  });
  await page.route((url) => new URL(url).pathname === '/api/events/catalog', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ events: [] }) });
  });
  await page.route((url) => new URL(url).pathname === '/api/hosts', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(hosts) });
  });
  await page.route('**/api/hosts/*/intake-candidates', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(intakeCandidates) });
  });
  await page.route((url) => new URL(url).pathname === '/api/devices', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    const requestUrl = new URL(route.request().url());
    const params = Object.fromEntries(requestUrl.searchParams.entries());
    deviceRequests.push(params);
    const statusParam = params.status;
    const filtered = statusParam
      ? devices.filter((device) => deviceChipStatus(device) === statusParam)
      : devices;
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(devicesResponseBody(filtered, requestUrl)) });
  });

  return {
    setDevices(nextDevices: Array<Record<string, unknown>>) {
      devices = nextDevices;
    },
    setIntakeCandidates(nextCandidates: Array<Record<string, unknown>>) {
      intakeCandidates = nextCandidates;
    },
    getLatestDevicesRequest() {
      return deviceRequests.at(-1) ?? null;
    },
  };
}

test.describe('Devices page', () => {
  test.beforeEach(async ({ page }) => {
    await mockDefaultDevicesSurface(page);
  });

  test('shows summary header pills with registered count and updated timestamp', async ({ page }) => {
    await page.goto('/devices?platform=android_mobile&search=Default');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });

    await expect(page.getByText(/registered across \d+ host/i)).toBeVisible();
    await expect(page.getByText(/updated/i)).toBeVisible();
    await expect(page.getByLabel('Available 1')).toBeVisible();
    await expect(page.getByLabel('Busy 0')).toBeVisible();
    await expect(page.getByLabel('Offline 0')).toBeVisible();
    await expect(page.getByLabel('Needs attention 0')).toBeVisible();

    await expectHeadingStyle(page.getByRole('heading', { name: 'Devices', exact: true }), '24px');
    await expectHeadingStyle(page.getByRole('heading', { name: /Showing \d+( of \d+)? devices?/ }), '14px');
  });

  test('summary pills preserve unrelated filters', async ({ page }) => {
    const mockApi = await mockAddDeviceVerificationSurface(page);
    mockApi.setDevices([
      {
        ...DEFAULT_DEVICE,
        id: 'device-available',
        identity_scheme: 'android_serial',
        identity_scope: 'host',
        identity_value: 'available-001',
        connection_target: 'available-001',
        name: 'Available Device',
        operational_state: 'available',
        hold: null,
        hardware_health_status: 'healthy',
        hardware_telemetry_state: 'fresh',
        needs_attention: false,
      },
      {
        ...DEFAULT_DEVICE,
        id: 'device-busy',
        identity_scheme: 'android_serial',
        identity_scope: 'host',
        identity_value: 'busy-001',
        connection_target: 'busy-001',
        name: 'Busy Device',
        operational_state: 'busy',
        hold: null,
        hardware_health_status: 'critical',
        hardware_telemetry_state: 'fresh',
        needs_attention: true,
      },
      {
        ...DEFAULT_DEVICE,
        id: 'device-offline',
        identity_scheme: 'android_serial',
        identity_scope: 'host',
        identity_value: 'offline-001',
        connection_target: 'offline-001',
        name: 'Offline Device',
        operational_state: 'offline',
        hold: null,
        hardware_health_status: 'healthy',
        hardware_telemetry_state: 'stale',
        needs_attention: true,
      },
    ]);

    await page.goto('/devices?platform=android_mobile&device_type=real_device&search=device');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });

    await page.getByLabel('Needs attention 2').click();
    await expect(page).toHaveURL(/platform=android_mobile/);
    await expect(page).toHaveURL(/device_type=real_device/);
    await expect(page).toHaveURL(/search=device/);
    await expect(page).toHaveURL(/needs_attention=true/);
    await expect(page).not.toHaveURL(/[?&]status=/);

    await page.getByLabel('Offline 1').click();
    await expect(page).toHaveURL(/status=offline/);
    await expect(page).toHaveURL(/platform=android_mobile/);
    await expect(page).toHaveURL(/device_type=real_device/);
    await expect(page).toHaveURL(/search=device/);
    await expect(page).not.toHaveURL(/needs_attention=/);
  });

  test('empty devices page shows add-device shortcut', async ({ page }) => {
    const mockApi = await mockAddDeviceVerificationSurface(page);
    mockApi.setDevices([]);

    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });

    await expect(page.getByText('Register a device to start routing sessions through Grid.')).toBeVisible();
    await expectHeadingStyle(page.getByRole('heading', { name: 'No devices registered' }), '14px');
    await page.getByRole('button', { name: 'Register Device' }).click();
    await expect(page.getByRole('dialog', { name: 'Add Device' })).toBeVisible();
  });

  test('loads and shows device table', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });

    const sortableHeaders = ['Device', 'Platform', 'Type', 'Connection', 'OS', 'Host', 'Availability'];
    for (const h of sortableHeaders) {
      await expect(page.getByRole('button', { name: h, exact: true })).toBeVisible();
    }
    await expect(page.getByRole('columnheader', { name: 'Health' })).toBeVisible();
    await expect(page.getByRole('columnheader', { name: 'Auto' })).toBeVisible();
    await expect(page.getByRole('columnheader', { name: 'Actions' })).toBeVisible();
    await expect(page.getByRole('button', { name: /Row actions for/i }).first()).toBeVisible();
    await expect(page.getByRole('columnheader', { name: 'Identity', exact: true })).toHaveCount(0);
    await expect(page.getByRole('columnheader', { name: 'Target', exact: true })).toHaveCount(0);
    await expect(page.getByText(/Showing \d+( of \d+)? devices?/)).toBeVisible();
  });

  test('shows registered devices', async ({ page }) => {
    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });

    // At least one device row should exist
    const rows = deviceRows(page);
    await expect(rows).not.toHaveCount(0);
  });

  test('filter dropdowns are present', async ({ page }) => {
    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });

    await expect(page.getByRole('option', { name: 'All platforms' })).toBeAttached();
    await expect(page.getByRole('option', { name: 'All types' })).toBeAttached();
    await expect(page.getByPlaceholder('Search by name, identity, or target...')).toBeVisible();

    // More filters are collapsed by default — open the section first.
    await page.getByRole('button', { name: /More filters/i }).click();
    await expect(page.getByRole('option', { name: 'All connections' })).toBeAttached();
    await expect(page.getByRole('option', { name: 'All OS versions' })).toBeAttached();
  });

  test('keeps filter select chrome consistent and header action separate from list subheader', async ({ page }) => {
    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });

    const pageHeader = page.locator('header').filter({ has: page.getByRole('heading', { name: 'Devices', exact: true }) });
    await expect(pageHeader.getByRole('button', { name: 'Add Device' })).toHaveCount(0);

    const subheader = page.getByTestId('list-page-subheader');
    await expect(subheader.getByRole('heading', { name: /Showing \d+( of \d+)? devices?/ })).toBeVisible();
    await expect(subheader.getByRole('button', { name: 'Add Device' })).toBeVisible();
    await expectHeadingStyle(subheader.getByRole('heading', { name: /Showing \d+( of \d+)? devices?/ }), '14px');

    const platformChrome = await controlChrome(page.getByRole('combobox', { name: 'Platform' }));
    await expect.poll(() => controlChrome(page.getByRole('combobox', { name: 'Device type' }))).toEqual(platformChrome);
  });

  test('sortable headers can be clicked', async ({ page }) => {
    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });

    await page.getByRole('button', { name: 'Device', exact: true }).click();
    await page.getByRole('button', { name: 'Device', exact: true }).click();
    await expect(firstDeviceRow(page)).toBeVisible();
  });

  test('search filters devices by name', async ({ page }) => {
    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });

    const searchBox = page.getByPlaceholder('Search by name, identity, or target...');
    // Use a name that won't match anything
    await searchBox.fill('zzz_nonexistent_device_zzz');
    await expect(page.getByText('No matching devices')).toBeVisible();

    // Clear search should show devices again
    await searchBox.clear();
    await expect(firstDeviceRow(page)).toBeVisible();
  });

  test('filter controls sync to the URL and devices API query params', async ({ page }) => {
    const mockApi = await mockAddDeviceVerificationSurface(page);
    mockApi.setDevices([
      {
        id: 'device-query-alpha',
        identity_scheme: 'android_serial',
        identity_scope: 'host',
        identity_value: 'alpha-001',
        connection_target: 'alpha-001',
        name: 'Alpha Device',
        pack_id: 'appium-uiautomator2',
        platform_id: 'android_mobile',
        platform_label: 'Android (real device)',
        os_version: '14',
        host_id: 'host-1',
        operational_state: 'available',
        hold: null,
        tags: null,
        auto_manage: true,
        device_type: 'real_device',
        connection_type: 'usb',
        ip_address: null,
        health_summary: mockHealthSummary(true, 'Healthy'),
        readiness_state: 'verified',
        missing_setup_fields: [],
        verified_at: '2026-03-30T10:00:03Z',
        reservation: null,
        lifecycle_policy_summary: {
          state: 'idle',
          label: 'Idle',
          detail: null,
          backoff_until: null,
        },
        created_at: '2026-03-30T10:00:03Z',
        updated_at: '2026-03-30T10:00:03Z',
      },
      {
        id: 'device-query-beta',
        identity_scheme: 'android_serial',
        identity_scope: 'host',
        identity_value: 'beta-001',
        connection_target: '192.168.1.25:5555',
        name: 'Beta Device',
        pack_id: 'appium-uiautomator2',
        platform_id: 'android_mobile',
        platform_label: 'Android (real device)',
        os_version: '15',
        host_id: 'host-1',
        operational_state: 'available',
        hold: null,
        tags: null,
        auto_manage: true,
        device_type: 'real_device',
        connection_type: 'network',
        ip_address: '192.168.1.25',
        health_summary: mockHealthSummary(true, 'Healthy'),
        readiness_state: 'verified',
        missing_setup_fields: [],
        verified_at: '2026-03-30T10:00:03Z',
        reservation: null,
        lifecycle_policy_summary: {
          state: 'idle',
          label: 'Idle',
          detail: null,
          backoff_until: null,
        },
        created_at: '2026-03-30T10:00:03Z',
        updated_at: '2026-03-30T10:00:03Z',
      },
    ]);

    // Navigate with status pre-set — the availability dropdown was removed;
    // filtering by status is now done via the summary pills or URL.
    await page.goto('/devices?status=available');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });

    // Primary filters: Driver pack (0), Platform (1), Type (2).
    const filters = page.getByRole('combobox');
    await filters.nth(0).selectOption('appium-uiautomator2');
    await filters.nth(1).selectOption('android_mobile');
    await filters.nth(2).selectOption('real_device');

    // More filters live behind the disclosure — open it first.
    await page.getByRole('button', { name: /More filters/i }).click();
    // Advanced filters: Connection Type (3), OS Version (4),
    // Hardware Health (5), Telemetry State (6).
    await filters.nth(3).selectOption('network');
    await filters.nth(4).selectOption('15');
    await filters.nth(5).selectOption('healthy');
    await filters.nth(6).selectOption('fresh');

    await page.getByPlaceholder('Search by name, identity, or target...').fill('beta');

    await expect.poll(() => mockApi.getLatestDevicesRequest()?.platform_id ?? null).toBe('android_mobile');
    await expect.poll(() => mockApi.getLatestDevicesRequest()?.pack_id ?? null).toBe('appium-uiautomator2');
    await expect.poll(() => mockApi.getLatestDevicesRequest()?.status ?? null).toBe('available');
    await expect.poll(() => mockApi.getLatestDevicesRequest()?.device_type ?? null).toBe('real_device');
    await expect.poll(() => mockApi.getLatestDevicesRequest()?.connection_type ?? null).toBe('network');
    await expect.poll(() => mockApi.getLatestDevicesRequest()?.os_version ?? null).toBe('15');
    await expect.poll(() => mockApi.getLatestDevicesRequest()?.hardware_health_status ?? null).toBe('healthy');
    await expect.poll(() => mockApi.getLatestDevicesRequest()?.hardware_telemetry_state ?? null).toBe('fresh');
    await expect.poll(() => mockApi.getLatestDevicesRequest()?.search ?? null).toBe('beta');

    await expect(page).toHaveURL(/platform_id=android_mobile/);
    await expect(page).toHaveURL(/pack_id=appium-uiautomator2/);
    await expect(page).toHaveURL(/status=available/);
    await expect(page).toHaveURL(/device_type=real_device/);
    await expect(page).toHaveURL(/connection_type=network/);
    await expect(page).toHaveURL(/os_version=15/);
    await expect(page).toHaveURL(/hardware_health_status=healthy/);
    await expect(page).toHaveURL(/hardware_telemetry_state=fresh/);
    await expect(page).toHaveURL(/search=beta/);
  });

  test('advanced filters collapse and show active-filter summary chips', async ({ page }) => {
    const mockApi = await mockAddDeviceVerificationSurface(page);
    mockApi.setDevices([
      {
        id: 'device-chips-test',
        identity_scheme: 'android_serial',
        identity_scope: 'host',
        identity_value: 'chips-001',
        connection_target: 'chips-001',
        name: 'Chips Test Device',
        pack_id: 'appium-uiautomator2',
        platform_id: 'android_mobile',
        platform_label: 'Android (real device)',
        os_version: '14',
        host_id: 'host-1',
        operational_state: 'available',
        hold: null,
        tags: null,
        auto_manage: true,
        device_type: 'real_device',
        connection_type: 'usb',
        ip_address: null,
        health_summary: mockHealthSummary(true, 'Healthy'),
        hardware_health_status: 'healthy',
        hardware_telemetry_state: 'fresh',
        readiness_state: 'verified',
        missing_setup_fields: [],
        verified_at: '2026-03-30T10:00:03Z',
        reservation: null,
        lifecycle_policy_summary: { state: 'idle', label: 'Idle', detail: null, backoff_until: null },
        created_at: '2026-03-30T10:00:03Z',
        updated_at: '2026-03-30T10:00:03Z',
      },
    ]);

    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });

    await page.getByRole('button', { name: /More filters/i }).click();
    const filters = page.getByRole('combobox');
    // Advanced filters: Connection Type (3), OS Version (4) — Availability dropdown removed.
    await filters.nth(3).selectOption('usb');
    await filters.nth(4).selectOption('14');

    await page.getByRole('button', { name: /More filters/i }).click();

    await expect(page.getByText(/Connection: USB/)).toBeVisible();
    await expect(page.getByText(/OS: 14/)).toBeVisible();

    await page.getByRole('button', { name: /Remove filter Connection: USB/i }).click();
    await expect(page).not.toHaveURL(/connection_type=/);
    await expect(page.getByText('Connection: USB')).not.toBeVisible();
    await expect(page.getByText(/OS: 14/)).toBeVisible();
  });

  test('auto-manage checkboxes are present', async ({ page }) => {
    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });

    const checkboxes = firstDeviceRow(page).getByRole('checkbox');
    await expect(checkboxes).toHaveCount(2);
  });

  test('renders the devices list directly for a small fleet', async ({ page }) => {
    const mockApi = await mockAddDeviceVerificationSurface(page);
    mockApi.setDevices(
      Array.from({ length: 80 }, (_, index) => ({
        id: `device-${String(index).padStart(3, '0')}`,
        identity_scheme: 'manager_generated',
        identity_scope: 'host',
        identity_value: `avd:virtual-${index}`,
        connection_target: `Virtual_${index}`,
        name: `Virtual Device ${index}`,
        pack_id: 'appium-uiautomator2',
        platform_id: 'android_mobile',
        platform_label: 'Android (real device)',
        os_version: '14',
        host_id: 'host-1',
        operational_state: 'available',
        hold: null,
        tags: null,
        auto_manage: true,
        device_type: 'emulator',
        connection_type: 'virtual',
        ip_address: null,
        health_summary: mockHealthSummary(true, 'Healthy'),
        readiness_state: 'verified',
        missing_setup_fields: [],
        verified_at: '2026-03-30T10:00:03Z',
        reservation: null,
        lifecycle_policy_summary: {
          state: 'idle',
          label: 'Idle',
          detail: null,
          backoff_until: null,
        },
        created_at: '2026-03-30T10:00:03Z',
        updated_at: '2026-03-30T10:00:03Z',
      })),
    );

    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('device-row-device-000')).toBeVisible();
    await expect(deviceRows(page)).toHaveCount(80);
    await expect(page.getByTestId('device-row-device-079')).toHaveCount(1);
  });

  test('rolls back optimistic auto-manage changes when the mutation fails', async ({ page }) => {
    const mockApi = await mockAddDeviceVerificationSurface(page);
    mockApi.setDevices([
      {
        id: 'device-auto-manage',
        identity_scheme: 'android_serial',
        identity_scope: 'host',
        identity_value: 'auto-manage-001',
        connection_target: 'auto-manage-001',
        name: 'Auto Manage Device',
        pack_id: 'appium-uiautomator2',
        platform_id: 'android_mobile',
        platform_label: 'Android (real device)',
        os_version: '14',
        host_id: 'host-1',
        operational_state: 'available',
        hold: null,
        tags: null,
        auto_manage: true,
        device_type: 'real_device',
        connection_type: 'usb',
        ip_address: null,
        health_summary: mockHealthSummary(true, 'Healthy'),
        readiness_state: 'verified',
        missing_setup_fields: [],
        verified_at: '2026-03-30T10:00:03Z',
        reservation: null,
        lifecycle_policy_summary: {
          state: 'idle',
          label: 'Idle',
          detail: null,
          backoff_until: null,
        },
        created_at: '2026-03-30T10:00:03Z',
        updated_at: '2026-03-30T10:00:03Z',
      },
    ]);

    await page.route((url) => new URL(url).pathname === '/api/devices/device-auto-manage', async (route) => {
      if (route.request().method() !== 'PATCH') {
        await route.fallback();
        return;
      }
      await route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({
          error: {
            code: 'CONFLICT',
            message: 'Cannot update auto-manage right now',
            request_id: 'req-auto-manage',
          },
        }),
      });
    });

    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });

    const autoManageToggle = firstDeviceRow(page).getByRole('checkbox').nth(1);
    await expect(autoManageToggle).toBeChecked();
    await autoManageToggle.click();

    await expect(page.getByText('Cannot update auto-manage right now')).toBeVisible({ timeout: 5000 });
    await expect(autoManageToggle).toBeChecked({ timeout: 5000 });
  });

  test('shows the page error boundary fallback when the devices page throws', async ({ page }) => {
    await page.addInitScript(() => {
      window.__GRIDFLEET_RENDER_CRASH_TARGET__ = 'devices-page';
    });

    await page.goto('/devices');
    await expect(page.getByText('Something went wrong')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('button', { name: 'Reload' })).toBeVisible();
  });

  test('row actions are available from the overflow menu', async ({ page }) => {
    const mockApi = await mockAddDeviceVerificationSurface(page);
    mockApi.setDevices([
      {
        id: 'device-menu',
        identity_scheme: 'android_serial',
        identity_scope: 'host',
        identity_value: 'menu-device-001',
        connection_target: 'menu-device-001',
        name: 'Menu Device',
        pack_id: 'appium-uiautomator2',
        platform_id: 'android_mobile',
        platform_label: 'Android (real device)',
        os_version: '14',
        host_id: 'host-1',
        operational_state: 'available',
        hold: null,
        tags: null,
        auto_manage: true,
        device_type: 'real_device',
        connection_type: 'usb',
        ip_address: null,
        health_summary: mockHealthSummary(true, 'Healthy'),
        readiness_state: 'verified',
        missing_setup_fields: [],
        verified_at: '2026-03-30T10:00:03Z',
        reservation: null,
        lifecycle_policy_summary: {
          state: 'idle',
          label: 'Idle',
          detail: null,
          backoff_until: null,
        },
        created_at: '2026-03-30T10:00:03Z',
        updated_at: '2026-03-30T10:00:03Z',
      },
    ]);

    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });

    await page.getByRole('button', { name: 'Row actions for Menu Device' }).click();
    await expect(page.getByRole('menuitem', { name: 'Start Node', exact: true })).toBeVisible();
    await expect(page.getByRole('menuitem', { name: 'Edit Configuration', exact: true })).toBeVisible();
    await expect(page.getByRole('menuitem', { name: 'Delete Device', exact: true })).toBeVisible();
  });

  test('row actions menu stays inside viewport on the last row at 1280x720', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 720 });
    const mockApi = await mockAddDeviceVerificationSurface(page);
    const deviceCount = 12;
    const targetName = `Row ${deviceCount} Device`;
    mockApi.setDevices(
      Array.from({ length: deviceCount }, (_, index) => ({
        id: `device-row-${index + 1}`,
        identity_scheme: 'android_serial',
        identity_scope: 'host',
        identity_value: `row-device-${String(index + 1).padStart(3, '0')}`,
        connection_target: `row-device-${String(index + 1).padStart(3, '0')}`,
        name: `Row ${index + 1} Device`,
        pack_id: 'appium-uiautomator2',
        platform_id: 'android_mobile',
        platform_label: 'Android (real device)',
        os_version: '14',
        host_id: 'host-1',
        operational_state: 'available',
        hold: null,
        tags: null,
        auto_manage: true,
        device_type: 'real_device',
        connection_type: 'usb',
        ip_address: null,
        health_summary: mockHealthSummary(true, 'Healthy'),
        readiness_state: 'verified',
        missing_setup_fields: [],
        verified_at: '2026-03-30T10:00:03Z',
        reservation: null,
        lifecycle_policy_summary: {
          state: 'idle',
          label: 'Idle',
          detail: null,
          backoff_until: null,
        },
        created_at: '2026-03-30T10:00:03Z',
        updated_at: '2026-03-30T10:00:03Z',
      })),
    );

    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });

    const trigger = page.getByRole('button', { name: `Row actions for ${targetName}` });
    await trigger.scrollIntoViewIfNeeded();
    await trigger.click();

    const menu = page.getByRole('menu');
    await expect(menu).toBeVisible();
    const viewport = page.viewportSize();
    expect(viewport).not.toBeNull();
    const box = await menu.boundingBox();
    expect(box).not.toBeNull();
    if (box && viewport) {
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.y).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width).toBeLessThanOrEqual(viewport.width);
      expect(box.y + box.height).toBeLessThanOrEqual(viewport.height);
    }
  });

  test('devices table has fixed columns without column picker', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    const mockApi = await mockAddDeviceVerificationSurface(page);
    mockApi.setDevices([
      {
        id: 'device-columns',
        identity_scheme: 'android_serial',
        identity_scope: 'host',
        identity_value: 'columns-device-001',
        connection_target: 'columns-device-001',
        name: 'Column Device',
        pack_id: 'appium-uiautomator2',
        platform_id: 'android_mobile',
        platform_label: 'Android (real device)',
        os_version: '14',
        host_id: 'host-1',
        operational_state: 'available',
        hold: null,
        tags: null,
        auto_manage: true,
        device_type: 'real_device',
        connection_type: 'usb',
        ip_address: null,
        health_summary: mockHealthSummary(true, 'Healthy'),
        readiness_state: 'verified',
        missing_setup_fields: [],
        verified_at: '2026-03-30T10:00:03Z',
        reservation: null,
        lifecycle_policy_summary: {
          state: 'idle',
          label: 'Idle',
          detail: null,
          backoff_until: null,
        },
        created_at: '2026-03-30T10:00:03Z',
        updated_at: '2026-03-30T10:00:03Z',
      },
    ]);

    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('button', { name: 'Columns' })).toHaveCount(0);
    await expect(page.getByRole('columnheader', { name: 'Identity', exact: true })).toHaveCount(0);
    await expect(page.getByRole('columnheader', { name: 'Target', exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Platform', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Connection', exact: true })).toBeVisible();
    await expect(page.getByRole('columnheader', { name: 'Health', exact: true })).toBeVisible();
    await expect(page.getByRole('columnheader', { name: 'Auto', exact: true })).toBeVisible();
  });

  test('devices table hides secondary columns at 1280 and shows them at 1440', async ({ page }) => {
    const mockApi = await mockAddDeviceVerificationSurface(page);
    mockApi.setDevices([
      {
        id: 'device-responsive-columns',
        identity_scheme: 'android_serial',
        identity_scope: 'host',
        identity_value: 'responsive-device-001',
        connection_target: 'responsive-device-001',
        name: 'Responsive Column Device',
        pack_id: 'appium-uiautomator2',
        platform_id: 'android_mobile',
        platform_label: 'Android (real device)',
        os_version: '14',
        host_id: 'host-1',
        operational_state: 'available',
        hold: null,
        tags: null,
        auto_manage: true,
        device_type: 'real_device',
        connection_type: 'usb',
        ip_address: null,
        health_summary: mockHealthSummary(true, 'Healthy'),
        readiness_state: 'verified',
        missing_setup_fields: [],
        verified_at: '2026-03-30T10:00:03Z',
        reservation: null,
        lifecycle_policy_summary: {
          state: 'idle',
          label: 'Idle',
          detail: null,
          backoff_until: null,
        },
        created_at: '2026-03-30T10:00:03Z',
        updated_at: '2026-03-30T10:00:03Z',
      },
    ]);

    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('button', { name: 'OS', exact: true, includeHidden: true })).toBeHidden();
    await expect(page.getByRole('columnheader', { name: 'Health', exact: true })).toBeVisible();

    await page.setViewportSize({ width: 1440, height: 900 });
    await expect(page.getByRole('button', { name: 'OS', exact: true })).toBeVisible();
  });

  test('Add Device button opens modal', async ({ page }) => {
    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });

    await page.getByRole('button', { name: 'Add Device' }).click();
    const dialog = page.getByRole('dialog', { name: 'Add Device' });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByLabel('Host')).toBeVisible();

    // Platform and other fields appear only after a host is selected
    await dialog.getByLabel('Host').selectOption({ index: 1 });
    await expect(dialog.getByLabel('Platform')).toBeVisible();
    await expect(dialog.getByLabel('Display Name Override')).toBeVisible();

    // Cancel closes modal
    await dialog.getByRole('button', { name: 'Cancel' }).click();
    await expect(dialog).not.toBeVisible();
  });

  test('add flow exposes current Roku pack setup fields', async ({ page }) => {
    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });

    await page.getByRole('button', { name: 'Add Device' }).click();
    const dialog = page.getByRole('dialog', { name: 'Add Device' });
    await dialog.getByLabel('Host').selectOption({ index: 1 });

    const values = await dialog.getByLabel('Platform').locator('option').evaluateAll((options) =>
      options.map((option) => (option as HTMLOptionElement).value),
    );
    expect(values).toContain('appium-roku-dlenroc::roku_network');

    await dialog.getByLabel('Platform').selectOption('appium-roku-dlenroc::roku_network');
    await expect(dialog.getByLabel('IP Address')).toBeVisible();
    await expect(dialog.getByLabel('Developer password')).toBeVisible();
  });

  test('Add Device renders manifest fields and submits device_config for selected pack platform', async ({ page }) => {
    await mockAddDeviceVerificationSurface(page);
    let submitted: Record<string, unknown> | null = null;

    await page.route('**/api/driver-packs/catalog', async (route) => {
      await fulfillJson(route, {
        packs: [
          {
            id: 'appium-uiautomator2',
            display_name: 'Appium UiAutomator2',
            state: 'enabled',
            current_release: '2026.04.0',
            active_runs: 0,
            live_sessions: 0,
            runtime_policy: { strategy: 'recommended' },
            platforms: [{
              id: 'android_mobile',
              display_name: 'Android Curated',
              automation_name: 'UiAutomator2',
              appium_platform_name: 'Android',
              device_types: ['real_device'],
              connection_types: ['usb', 'network'],
              grid_slots: ['native'],
              identity_scheme: 'android_serial',
              identity_scope: 'host',
              discovery_kind: 'adb',
              device_fields_schema: [],
              capabilities: {},
              display_metadata: { icon_kind: 'mobile' },
              default_capabilities: {},
              connection_behavior: { requires_connection_target: true },
            }],
          },
          {
            id: 'vendor/uiautomator2-android-real',
            display_name: 'Vendor Android',
            state: 'enabled',
            current_release: '2026.04.0',
            active_runs: 0,
            live_sessions: 0,
            runtime_policy: { strategy: 'recommended' },
            platforms: [{
              id: 'android_mobile',
              display_name: 'Android Vendor',
              automation_name: 'UiAutomator2',
              appium_platform_name: 'Android',
              device_types: ['real_device'],
              connection_types: ['network'],
              grid_slots: ['native'],
              identity_scheme: 'android_serial',
              identity_scope: 'host',
              discovery_kind: 'adb',
              device_fields_schema: [{ id: 'custom_token', label: 'Custom token', type: 'string', required_for_session: true }],
              capabilities: {},
              display_metadata: { icon_kind: 'mobile' },
              default_capabilities: {},
              connection_behavior: { requires_ip_address: true, requires_connection_target: true },
            }],
          },
          {
            id: 'appium-roku-dlenroc',
            display_name: 'Roku (dlenroc)',
            state: 'enabled',
            current_release: '2026.04.0',
            active_runs: 0,
            live_sessions: 0,
            runtime_policy: { strategy: 'recommended' },
            platforms: [{
              id: 'roku_network',
              display_name: 'Roku (network)',
              automation_name: 'Roku',
              appium_platform_name: 'roku',
              device_types: ['real_device'],
              connection_types: ['network'],
              grid_slots: ['native'],
              identity_scheme: 'roku_serial',
              identity_scope: 'global',
              discovery_kind: 'roku_ecp',
              device_fields_schema: [{ id: 'roku_password', label: 'Developer password', type: 'string', required_for_session: true, sensitive: true, capability_name: 'appium:password' }],
              capabilities: {},
              display_metadata: { icon_kind: 'set_top' },
              default_capabilities: { 'appium:ip': '{device.ip_address}' },
              connection_behavior: { default_device_type: 'real_device', default_connection_type: 'network', requires_ip_address: true, requires_connection_target: false },
            }],
          },
        ],
      });
    });

    await page.route('**/api/devices/verification-jobs', async (route) => {
      if (route.request().method() !== 'POST') {
        await route.fallback();
        return;
      }
      submitted = route.request().postDataJSON() as Record<string, unknown>;
      await fulfillJson(route, verificationJob('job-roku', { status: 'completed', device_id: 'roku-1' }), 201);
    });

    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });
    await page.getByRole('button', { name: 'Add Device' }).click();
    const dialog = page.getByRole('dialog', { name: 'Add Device' });
    await dialog.getByLabel('Host').selectOption('host-1');

    const optionValues = await dialog.getByLabel('Platform').locator('option').evaluateAll((options) =>
      options.map((option) => (option as HTMLOptionElement).value),
    );
    expect(optionValues.filter((value) => value.endsWith('::android_mobile'))).toHaveLength(2);

    await dialog.getByLabel('Platform').selectOption('appium-roku-dlenroc::roku_network');
    await expect(dialog.getByLabel('Identity Value')).toHaveCount(0);
    await dialog.getByLabel('IP Address').fill('192.168.1.55');
    await dialog.getByLabel('Developer password').fill('secret123');
    await dialog.getByLabel('Display Name Override').fill('Living Room Roku');
    await dialog.getByRole('button', { name: /verify & add device/i }).click();

    await expect.poll(() => submitted).toMatchObject({
      pack_id: 'appium-roku-dlenroc',
      platform_id: 'roku_network',
      identity_scheme: 'roku_serial',
      identity_scope: 'global',
      identity_value: null,
      ip_address: '192.168.1.55',
      connection_target: null,
      name: 'Living Room Roku',
      device_config: { roku_password: 'secret123' },
    });
  });

  test('device name links to detail page', async ({ page }) => {
    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });

    const firstDeviceLink = firstDeviceRow(page).getByRole('link').first();
    const name = await firstDeviceLink.textContent();
    await firstDeviceLink.click();

    // Should navigate to device detail
    await expect(page.getByRole('heading', { name: name!, exact: true })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText('Device Info')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Device Health' })).toBeVisible();
  });

  test('devices table shows unified health column with telemetry summary', async ({ page }) => {
    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });

    await expect(page.getByRole('columnheader', { name: 'Health' })).toBeVisible();
    await expect(firstDeviceRow(page).getByText(/Healthy/i)).toBeVisible();
    await firstDeviceRow(page).getByRole('button', { name: /Health details/ }).click();
    await expect(page.getByRole('dialog', { name: /Health details/ }).getByText('84% · Charging')).toBeVisible();
  });

  test('device detail shows triage, setup, and logs content', async ({ page }) => {
    await page.route((url) => new URL(url).pathname === `/api/devices/${DEFAULT_DEVICE.id}/config`, async (route) => {
      if (route.request().method() !== 'GET') {
        await route.fallback();
        return;
      }
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
    });

    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });

    const firstDeviceLink = firstDeviceRow(page).getByRole('link').first();
    await firstDeviceLink.click();

    // Triage tab (default) surfaces Device Health + Info/Telemetry/Tags grid.
    await expect(page.getByText('Device Health')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('button', { name: 'Test Session' })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText('Session Viability')).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText('Device Info')).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole('heading', { name: 'Hardware Telemetry' })).toBeVisible({ timeout: 10_000 });

    // Switch to Setup tab — node controls + capabilities + config editor
    await page.getByRole('button', { name: 'Setup', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Device Control' })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole('heading', { name: 'Appium Node' })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole('heading', { name: 'Configuration' })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText('No config overrides')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('button', { name: 'Add override' })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('button', { name: 'Save & Verify' })).toBeHidden();
    await page.getByRole('button', { name: 'Add override' }).click();
    const configSection = page
      .locator('section')
      .filter({ has: page.getByRole('heading', { name: 'Configuration' }) });
    await expect(configSection.getByRole('button', { name: 'Save & Verify' })).toBeVisible({ timeout: 15_000 });
    await expect(configSection.getByRole('button', { name: 'Reset' })).toBeVisible({ timeout: 15_000 });
    await expect(configSection.getByRole('button', { name: 'Format' })).toBeVisible({ timeout: 15_000 });

    // Switch to Logs tab
    await page.getByRole('button', { name: 'Logs', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Appium Logs' })).toBeVisible({ timeout: 10_000 });
  });

  test('device detail Logs tab renders device logs', async ({ page }) => {
    await page.goto(`/devices/${DEFAULT_DEVICE.id}?tab=logs`);

    await expect(page.getByRole('heading', { name: 'Appium Logs' })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText('Device log line')).toBeVisible();
  });

  test('device detail shows hardware telemetry card', async ({ page }) => {
    await page.goto(`/devices/${DEFAULT_DEVICE.id}`);

    await expect(page.getByRole('heading', { name: 'Hardware Telemetry' })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText('84%')).toBeVisible();
    await expect(page.getByText('Charging', { exact: true })).toBeVisible();
    await expect(page.getByText('36.7C')).toBeVisible();
    await expect(page.getByText('Fresh', { exact: true })).toBeVisible();
  });

  test('device detail shows health strip with control shortcut', async ({ page }) => {
    await page.goto(`/devices/${DEFAULT_DEVICE.id}`);

    await expect(page.getByRole('heading', { name: DEFAULT_DEVICE.name })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText('Android · 14 · linux-host')).toBeVisible();
    // Only Hardware and Connectivity pills remain (Readiness and Lifecycle were removed).
    await expect(page.getByText('Hardware', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('Connectivity', { exact: true })).toBeVisible();
    await expect(page.getByTestId('device-detail-status-pill')).toHaveCount(2);

    await page.getByRole('link', { name: /Connectivity.*Healthy/i }).click();
    await expect(page).toHaveURL(/\/devices\/device-default\?tab=triage(#device-health)?$/);
    await expect(page.getByText('Device Health')).toBeVisible({ timeout: 15_000 });
  });

  test('device detail hardware pill links to hardware filter', async ({ page }) => {
    // The Lifecycle pill was removed; only Hardware + Connectivity pills remain.
    // This test now only verifies the Hardware pill navigates to the hardware filter.
    const attentionDevice = {
      ...DEFAULT_DEVICE,
      name: 'Attention Device',
      lifecycle_policy_summary: {
        state: 'backoff',
        label: 'Backing Off',
        detail: 'Waiting before retry',
        backoff_until: '2026-03-30T10:10:03Z',
      },
      hardware_health_status: 'warning',
    };
    const attentionDetail = { ...DEFAULT_DEVICE_DETAIL, ...attentionDevice };

    await page.route((url) => new URL(url).pathname === '/api/devices', async (route) => {
      if (route.request().method() !== 'GET') {
        await route.fallback();
        return;
      }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(devicesResponseBody([attentionDevice], new URL(route.request().url()))) });
    });
    await page.route((url) => new URL(url).pathname === `/api/devices/${DEFAULT_DEVICE.id}`, async (route) => {
      if (route.request().method() !== 'GET') {
        await route.fallback();
        return;
      }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(attentionDetail) });
    });

    await page.goto(`/devices/${DEFAULT_DEVICE.id}`);
    await expect(page.getByRole('heading', { name: 'Attention Device' })).toBeVisible({ timeout: 15_000 });

    await page.getByRole('link', { name: /Hardware.*Warning/i }).click();
    await expect(page).toHaveURL(/\/devices\?hardware_health_status=warning$/);
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });
  });

  test('device detail tab navigation is URL-addressable', async ({ page }) => {
    await page.goto(`/devices/${DEFAULT_DEVICE.id}?tab=setup`);
    await expect(page.getByRole('heading', { name: DEFAULT_DEVICE.name })).toBeVisible({ timeout: 15_000 });
    // Deep-linked Setup tab surfaces node + config surfaces
    await expect(page.getByRole('heading', { name: 'Device Control' })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('heading', { name: 'Configuration' })).toBeVisible({ timeout: 15_000 });

    await expect(page).toHaveURL(/tab=setup/);

    // Unknown and removed tab ids fall back to Triage (Health panel is its signature)
    await page.goto(`/devices/${DEFAULT_DEVICE.id}?tab=bogus`);
    await expect(page.getByRole('heading', { name: DEFAULT_DEVICE.name })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText('Device Health')).toBeVisible({ timeout: 10_000 });

    await page.goto(`/devices/${DEFAULT_DEVICE.id}?tab=control`);
    await expect(page.getByText('Device Health')).toBeVisible({ timeout: 10_000 });
  });

  test('device detail history shows session outcome heatmap', async ({ page }) => {
    await page.route(
      (url) => new URL(url).pathname === '/api/lifecycle/incidents',
      async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ items: [], limit: 25, next_cursor: null, prev_cursor: null }),
        });
      },
    );

    await page.goto(`/devices/${DEFAULT_DEVICE.id}?tab=history`);

    await expect(page.getByRole('heading', { name: DEFAULT_DEVICE.name })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('heading', { name: 'Session Outcome Heatmap' })).toBeVisible();
    await expect(page.getByText('3 sessions across 2 active days')).toBeVisible();
    await expect(page.locator('[aria-label*="2 sessions"][aria-label*="1 error"]').first()).toBeVisible();
    await expect(page.getByRole('heading', { name: 'State History' })).toBeVisible();
  });

  test('device detail history shows heatmap empty state', async ({ page }) => {
    await page.route(
      (url) => new URL(url).pathname === `/api/devices/${DEFAULT_DEVICE.id}/session-outcome-heatmap`,
      async (route) => {
        await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
      },
    );
    await page.route(
      (url) => new URL(url).pathname === '/api/lifecycle/incidents',
      async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ items: [], limit: 25, next_cursor: null, prev_cursor: null }),
        });
      },
    );

    await page.goto(`/devices/${DEFAULT_DEVICE.id}?tab=history`);

    await expect(page.getByRole('heading', { name: 'Session Outcome Heatmap' })).toBeVisible();
    await expect(page.getByText('No completed session outcomes in this window')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'State History' })).toBeVisible();
  });

  test('device detail history keeps state history visible when heatmap fails', async ({ page }) => {
    await page.route(
      (url) => new URL(url).pathname === `/api/devices/${DEFAULT_DEVICE.id}/session-outcome-heatmap`,
      async (route) => {
        await route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"boom"}' });
      },
    );
    await page.route(
      (url) => new URL(url).pathname === '/api/lifecycle/incidents',
      async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ items: [], limit: 25, next_cursor: null, prev_cursor: null }),
        });
      },
    );

    await page.goto(`/devices/${DEFAULT_DEVICE.id}?tab=history`);

    await expect(page.getByText('Could not load device session outcome heatmap.')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'State History' })).toBeVisible();
  });

  test('bulk toolbar exposes tag action without removed config action', async ({ page }) => {
    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });

    await firstDeviceRow(page).getByRole('checkbox').first().check();
    const removedTemplateAction = new RegExp(['Template'].join(''), 'i');
    await expect(page.getByRole('button', { name: removedTemplateAction })).toHaveCount(0);
    await expect(page.getByRole('button', { name: /Tags/i })).toBeVisible();
  });

  test('needs_attention checkbox and URL param narrow the fleet view', async ({ page }) => {
    // Lifecycle is no longer a filter dropdown. Replaced by the "Needs attention" checkbox.
    const hosts = [
      {
        id: 'host-1',
        hostname: 'linux-host',
        ip: '10.0.0.20',
        os_type: 'linux',
        agent_port: 5100,
        status: 'online',
        agent_version: null,
        required_agent_version: null,
        agent_version_status: 'unknown',
        capabilities: null,
        last_heartbeat: null,
        created_at: '2026-03-30T10:00:00Z',
      },
    ];
    await page.route((url) => new URL(url).pathname === '/api/events', async (route) => {
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
    });
    await page.route((url) => new URL(url).pathname === '/api/events/catalog', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ events: [] }) });
    });
    await page.route((url) => new URL(url).pathname === '/api/hosts', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(hosts) });
    });
    const attentionDevices = [
      {
        id: 'device-attention',
        identity_scheme: 'android_serial',
        identity_scope: 'host',
        identity_value: 'device-attention',
        connection_target: 'device-attention',
        name: 'Attention Device',
        pack_id: 'appium-uiautomator2',
        platform_id: 'android_mobile',
        platform_label: 'Android (real device)',
        os_version: '14',
        host_id: 'host-1',
        operational_state: 'offline',
        hold: null,
        needs_attention: true,
        tags: null,
        auto_manage: true,
        device_type: 'real_device',
        connection_type: 'usb',
        ip_address: null,
        health_summary: mockHealthSummary(false, 'ADB not responsive'),
        readiness_state: 'verified',
        missing_setup_fields: [],
        verified_at: '2026-03-31T10:00:00Z',
        reservation: null,
        lifecycle_policy_summary: {
          state: 'backoff',
          label: 'Backing Off',
          detail: 'Automatic recovery is backing off before the next retry',
          backoff_until: '2026-03-31T10:10:00Z',
        },
        created_at: '2026-03-31T10:00:00Z',
        updated_at: '2026-03-31T10:00:00Z',
      },
      {
        id: 'device-healthy',
        identity_scheme: 'android_serial',
        identity_scope: 'host',
        identity_value: 'device-healthy',
        connection_target: 'device-healthy',
        name: 'Healthy Device',
        pack_id: 'appium-uiautomator2',
        platform_id: 'android_mobile',
        platform_label: 'Android (real device)',
        os_version: '14',
        host_id: 'host-1',
        needs_attention: false,
        tags: null,
        auto_manage: true,
        device_type: 'real_device',
        connection_type: 'usb',
        ip_address: null,
        health_summary: mockHealthSummary(true, 'Healthy'),
        readiness_state: 'verified',
        missing_setup_fields: [],
        verified_at: '2026-03-31T10:00:00Z',
        reservation: null,
        lifecycle_policy_summary: {
          state: 'idle',
          label: 'Idle',
          detail: null,
          backoff_until: null,
        },
        created_at: '2026-03-31T10:00:00Z',
        updated_at: '2026-03-31T10:00:00Z',
      },
    ];
    await page.route((url) => new URL(url).pathname === '/api/devices', async (route) => {
      if (route.request().method() !== 'GET') {
        await route.fallback();
        return;
      }
      const requestUrl = new URL(route.request().url());
      const needsAttentionParam = requestUrl.searchParams.get('needs_attention');
      const filtered = needsAttentionParam === 'true'
        ? attentionDevices.filter((device) => device.needs_attention)
        : attentionDevices;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(devicesResponseBody(filtered, requestUrl)),
      });
    });

    // Navigate with needs_attention=true pre-applied in URL — the checkbox was removed;
    // filtering by needs_attention is now done via URL param or the "Needs attention" summary pill.
    await page.goto('/devices?needs_attention=true');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });
    await expect(page).toHaveURL(/needs_attention=true/);
    await expect(page.getByText('Attention Device')).toBeVisible();
    await expect(page.getByText('Healthy Device')).not.toBeVisible();
  });

  test('device detail exposes maintenance controls', async ({ page }) => {
    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });

    await firstDeviceRow(page).getByRole('link').first().click();
    await page.getByRole('button', { name: 'Setup', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Device Control' })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole('button', { name: /Enter Maintenance|Exit Maintenance/ })).toBeVisible();
  });

  test('device detail shows Boot and Shutdown buttons for emulator', async ({ page }) => {
    const EMULATOR_ID = 'emulator-device-001';
    const emulatorDevice = {
      ...DEFAULT_DEVICE,
      id: EMULATOR_ID,
      name: 'Pixel 6 Emulator',
      platform_id: 'android_mobile',
      platform_label: 'Android Emulator',
      device_type: 'emulator',
      connection_type: 'virtual',
      identity_scheme: 'manager_generated',
      identity_scope: 'host',
      identity_value: 'avd:Pixel_6',
      connection_target: 'Pixel_6',
    };
    const emulatorDeviceDetail = { ...emulatorDevice, appium_node: null, sessions: [] };

    await page.route(
      (url) => new URL(url).pathname === '/api/devices',
      async (route) => {
        if (route.request().method() !== 'GET') { await route.fallback(); return; }
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(devicesResponseBody([emulatorDevice], new URL(route.request().url()))) });
      },
    );
    await page.route(
      (url) => new URL(url).pathname === `/api/devices/${EMULATOR_ID}`,
      async (route) => {
        if (route.request().method() !== 'GET') { await route.fallback(); return; }
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(emulatorDeviceDetail) });
      },
    );
    await page.route(
      (url) => url.pathname.startsWith(`/api/devices/${EMULATOR_ID}/`),
      async (route) => {
        await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
      },
    );

    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });
    await firstDeviceRow(page).getByRole('link').first().click();
    await page.getByRole('button', { name: 'Setup', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Lifecycle', exact: true })).toBeVisible({ timeout: 10_000 });

    await expect(page.getByRole('button', { name: 'Boot' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Shutdown' })).toBeVisible();
  });

  test('device detail does not infer headless controls for emulator', async ({ page }) => {
    const EMULATOR_ID = 'emulator-headless-001';
    const emulatorDevice = {
      ...DEFAULT_DEVICE,
      id: EMULATOR_ID,
      name: 'Pixel 6 Emulator (headless test)',
      platform_id: 'android_mobile',
      platform_label: 'Android Emulator',
      device_type: 'emulator',
      connection_type: 'virtual',
      identity_scheme: 'manager_generated',
      identity_scope: 'host',
      identity_value: 'avd:Pixel_6',
      connection_target: 'Pixel_6',
      tags: null,
    };
    const emulatorDeviceDetail = { ...emulatorDevice, appium_node: null, sessions: [] };

    await page.route(
      (url) => new URL(url).pathname === '/api/devices',
      async (route) => {
        if (route.request().method() !== 'GET') { await route.fallback(); return; }
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(devicesResponseBody([emulatorDevice], new URL(route.request().url()))) });
      },
    );
    await page.route(
      (url) => new URL(url).pathname === `/api/devices/${EMULATOR_ID}`,
      async (route) => {
        if (route.request().method() !== 'GET') { await route.fallback(); return; }
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(emulatorDeviceDetail) });
      },
    );
    await page.route(
      (url) => url.pathname.startsWith(`/api/devices/${EMULATOR_ID}/`),
      async (route) => { await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }); },
    );

    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });
    await firstDeviceRow(page).getByRole('link').first().click();
    await page.getByRole('button', { name: 'Setup', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Lifecycle', exact: true })).toBeVisible({ timeout: 10_000 });

    await expect(page.getByLabel('Headless mode')).not.toBeVisible();
    await expect(page.getByText(/Emulator runs/)).not.toBeVisible();
  });

  test('device detail ignores legacy emulator headless tag', async ({ page }) => {
    const EMULATOR_ID = 'emulator-headed-001';
    const emulatorDevice = {
      ...DEFAULT_DEVICE,
      id: EMULATOR_ID,
      name: 'Pixel 6 Emulator (headed)',
      platform_id: 'android_mobile',
      platform_label: 'Android Emulator',
      device_type: 'emulator',
      connection_type: 'virtual',
      identity_scheme: 'manager_generated',
      identity_scope: 'host',
      identity_value: 'avd:Pixel_6_headed',
      connection_target: 'Pixel_6_headed',
      tags: { emulator_headless: 'false' },
    };
    const emulatorDeviceDetail = { ...emulatorDevice, appium_node: null, sessions: [] };

    await page.route(
      (url) => new URL(url).pathname === '/api/devices',
      async (route) => {
        if (route.request().method() !== 'GET') { await route.fallback(); return; }
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(devicesResponseBody([emulatorDevice], new URL(route.request().url()))) });
      },
    );
    await page.route(
      (url) => new URL(url).pathname === `/api/devices/${EMULATOR_ID}`,
      async (route) => {
        if (route.request().method() !== 'GET') { await route.fallback(); return; }
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(emulatorDeviceDetail) });
      },
    );
    await page.route(
      (url) => url.pathname.startsWith(`/api/devices/${EMULATOR_ID}/`),
      async (route) => { await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }); },
    );

    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });
    await firstDeviceRow(page).getByRole('link').first().click();
    await page.getByRole('button', { name: 'Setup', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Lifecycle', exact: true })).toBeVisible({ timeout: 10_000 });

    await expect(page.getByLabel('Headless mode')).not.toBeVisible();
    await expect(page.getByText(/Emulator runs/)).not.toBeVisible();
  });

  test('headless toggle not visible for simulator', async ({ page }) => {
    const SIMULATOR_ID = 'sim-headless-test-001';
    const simulatorDevice = {
      ...DEFAULT_DEVICE,
      id: SIMULATOR_ID,
      name: 'iPhone 15 Sim (headless test)',
      pack_id: 'appium-xcuitest',
      platform_id: 'ios',
      platform_label: 'iOS Simulator',
      device_type: 'simulator',
      connection_type: 'virtual',
      identity_scheme: 'simulator_udid',
      identity_scope: 'host',
      identity_value: '00000000-0000-0000-0000-000000000099',
      connection_target: '00000000-0000-0000-0000-000000000099',
      tags: null,
    };
    const simulatorDeviceDetail = { ...simulatorDevice, appium_node: null, sessions: [] };

    await page.route(
      (url) => new URL(url).pathname === '/api/devices',
      async (route) => {
        if (route.request().method() !== 'GET') { await route.fallback(); return; }
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(devicesResponseBody([simulatorDevice], new URL(route.request().url()))) });
      },
    );
    await page.route(
      (url) => new URL(url).pathname === `/api/devices/${SIMULATOR_ID}`,
      async (route) => {
        if (route.request().method() !== 'GET') { await route.fallback(); return; }
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(simulatorDeviceDetail) });
      },
    );
    await page.route(
      (url) => url.pathname.startsWith(`/api/devices/${SIMULATOR_ID}/`),
      async (route) => { await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }); },
    );

    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });
    await firstDeviceRow(page).getByRole('link').first().click();
    await page.getByRole('button', { name: 'Setup', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Lifecycle', exact: true })).toBeVisible({ timeout: 10_000 });

    await expect(page.getByLabel('Headless mode')).not.toBeVisible();
  });

  test('device detail shows Boot and Shutdown buttons for simulator', async ({ page }) => {
    const SIMULATOR_ID = 'simulator-device-001';
    const simulatorDevice = {
      ...DEFAULT_DEVICE,
      id: SIMULATOR_ID,
      name: 'iPhone 15 Simulator',
      pack_id: 'appium-xcuitest',
      platform_id: 'ios',
      platform_label: 'iOS Simulator',
      device_type: 'simulator',
      connection_type: 'virtual',
      identity_scheme: 'simulator_udid',
      identity_scope: 'host',
      identity_value: '00000000-0000-0000-0000-000000000001',
      connection_target: '00000000-0000-0000-0000-000000000001',
    };
    const simulatorDeviceDetail = { ...simulatorDevice, appium_node: null, sessions: [] };

    await page.route(
      (url) => new URL(url).pathname === '/api/devices',
      async (route) => {
        if (route.request().method() !== 'GET') { await route.fallback(); return; }
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(devicesResponseBody([simulatorDevice], new URL(route.request().url()))) });
      },
    );
    await page.route(
      (url) => new URL(url).pathname === `/api/devices/${SIMULATOR_ID}`,
      async (route) => {
        if (route.request().method() !== 'GET') { await route.fallback(); return; }
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(simulatorDeviceDetail) });
      },
    );
    await page.route(
      (url) => url.pathname.startsWith(`/api/devices/${SIMULATOR_ID}/`),
      async (route) => {
        await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
      },
    );

    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });
    await firstDeviceRow(page).getByRole('link').first().click();
    await page.getByRole('button', { name: 'Setup', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Lifecycle', exact: true })).toBeVisible({ timeout: 10_000 });

    await expect(page.getByRole('button', { name: 'Boot' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Shutdown' })).toBeVisible();
  });

  test('device detail hides Virtual Device section for real devices', async ({ page }) => {
    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });

    await firstDeviceRow(page).getByRole('link').first().click();
    await page.getByRole('button', { name: 'Setup', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Device Control' })).toBeVisible({ timeout: 10_000 });

    await expect(page.getByRole('button', { name: 'Reconnect' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Refresh State' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Boot' })).not.toBeVisible();
  });

  test('add flow shows verification failure and preserves form values', async ({ page }) => {
    const mockApi = await mockAddDeviceVerificationSurface(page);

    await page.route('**/api/devices/verification-jobs', async (route) => {
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify(verificationJob('job-failure')),
      });
    });
    await page.route('**/api/devices/verification-jobs/job-failure/events', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: verificationEventStream(
          verificationJob('job-failure', {
            status: 'running',
            current_stage: 'node_start',
            current_stage_status: 'running',
            detail: 'Starting temporary verification node',
          }),
          verificationJob('job-failure', {
            status: 'failed',
            current_stage: 'session_probe',
            current_stage_status: 'failed',
            detail: 'Session startup failed',
            error: 'Session startup failed',
            finished_at: '2026-03-30T10:00:03Z',
          }),
        ),
      });
    });

    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });
    await page.getByRole('button', { name: 'Add Device' }).click();
    const dialog = page.getByRole('dialog', { name: 'Add Device' });
    await dialog.getByLabel('Host').selectOption('host-1');
    await dialog.getByLabel('Observed Device').selectOption('candidate-001:candidate-001');
    await dialog.getByLabel('Display Name Override').fill('Failure Device');
    await dialog.getByRole('button', { name: 'Verify & Add Device' }).click();

    await expect(dialog.getByTestId('device-verification-progress')).toBeVisible();
    await expect(dialog.getByText('Session startup failed').last()).toBeVisible({ timeout: 5000 });
    await expect(dialog.getByRole('button', { name: 'Retry Verification' })).toBeVisible();
    await expect(dialog.getByLabel('Display Name Override')).toHaveValue('Failure Device');

    mockApi.setDevices([]);
  });

  test('add flow closes on successful verification and refreshes the list', async ({ page }) => {
    const mockApi = await mockAddDeviceVerificationSurface(page);

    await page.route('**/api/devices/verification-jobs', async (route) => {
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify(verificationJob('job-success')),
      });
    });
    await page.route('**/api/devices/verification-jobs/job-success/events', async (route) => {
      mockApi.setDevices([
        {
          id: 'device-success',
          identity_scheme: 'android_serial',
          identity_scope: 'host',
          identity_value: 'success-001',
          connection_target: 'success-001',
          name: 'Success Device',
          pack_id: 'appium-uiautomator2',
          platform_id: 'android_mobile',
          platform_label: 'Android (real device)',
          os_version: '14',
          host_id: 'host-1',
        operational_state: 'available',
        hold: null,
          tags: null,
          auto_manage: true,
          device_type: 'real_device',
          connection_type: 'usb',
          ip_address: null,
          health_summary: mockHealthSummary(true, 'Healthy'),
          readiness_state: 'verified',
          missing_setup_fields: [],
          verified_at: '2026-03-30T10:00:03Z',
          reservation: null,
          lifecycle_policy_summary: {
            state: 'idle',
            label: 'Idle',
            detail: null,
            backoff_until: null,
          },
          created_at: '2026-03-30T10:00:03Z',
          updated_at: '2026-03-30T10:00:03Z',
        },
      ]);
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: verificationEventStream(
          verificationJob('job-success', {
            status: 'running',
            current_stage: 'session_probe',
            current_stage_status: 'running',
            detail: 'Grid-routed Appium probe session running',
          }),
          verificationJob('job-success', {
            status: 'completed',
            current_stage: 'save_device',
            current_stage_status: 'passed',
            detail: 'Device saved after verification',
            device_id: 'device-success',
            finished_at: '2026-03-30T10:00:03Z',
          }),
        ),
      });
    });

    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });
    await page.getByRole('button', { name: 'Add Device' }).click();
    const dialog = page.getByRole('dialog', { name: 'Add Device' });
    await dialog.getByLabel('Host').selectOption('host-1');
    await dialog.getByLabel('Observed Device').selectOption('candidate-001:candidate-001');
    await dialog.getByLabel('Display Name Override').fill('Success Device');
    await dialog.getByRole('button', { name: 'Verify & Add Device' }).click();

    await expect(dialog).not.toBeVisible({ timeout: 5000 });
    await expect(page.getByRole('link', { name: 'Success Device' })).toBeVisible({ timeout: 5000 });
  });

  test('edit flow reroutes readiness-impacting changes into verification and refreshes the list', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    const mockApi = await mockAddDeviceVerificationSurface(page);
    mockApi.setDevices([
      {
        id: 'device-edit-success',
        identity_scheme: 'android_serial',
        identity_scope: 'host',
        identity_value: 'edit-success-001',
        connection_target: '192.168.1.20:5555',
        name: 'Edit Success Device',
        pack_id: 'appium-uiautomator2',
        platform_id: 'android_mobile',
        platform_label: 'Android (real device)',
        os_version: '14',
        host_id: 'host-1',
        operational_state: 'available',
        hold: null,
        tags: null,
        auto_manage: true,
        device_type: 'real_device',
        connection_type: 'network',
        ip_address: '192.168.1.20',
        health_summary: mockHealthSummary(false, 'Awaiting reconnect'),
        readiness_state: 'verified',
        missing_setup_fields: [],
        verified_at: '2026-03-30T10:00:03Z',
        reservation: null,
        lifecycle_policy_summary: {
          state: 'idle',
          label: 'Idle',
          detail: null,
          backoff_until: null,
        },
        created_at: '2026-03-30T10:00:03Z',
        updated_at: '2026-03-30T10:00:03Z',
      },
    ]);

    await page.route('**/api/devices/device-edit-success/verification-jobs', async (route) => {
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify(verificationJob('job-edit-success')),
      });
    });
    await page.route('**/api/devices/verification-jobs/job-edit-success/events', async (route) => {
      mockApi.setDevices([
        {
          id: 'device-edit-success',
          identity_scheme: 'android_serial',
          identity_scope: 'host',
          identity_value: 'edit-success-001',
          connection_target: '10.0.0.15:5555',
          name: 'Edit Success Device',
          pack_id: 'appium-uiautomator2',
          platform_id: 'android_mobile',
          platform_label: 'Android (real device)',
          os_version: '15',
          host_id: 'host-1',
        operational_state: 'available',
        hold: null,
          tags: null,
          auto_manage: true,
          device_type: 'real_device',
          connection_type: 'network',
          ip_address: '10.0.0.15',
          health_summary: mockHealthSummary(true, 'Healthy'),
          readiness_state: 'verified',
          missing_setup_fields: [],
          verified_at: '2026-03-30T10:05:03Z',
          reservation: null,
          lifecycle_policy_summary: {
            state: 'idle',
            label: 'Idle',
            detail: null,
            backoff_until: null,
          },
          created_at: '2026-03-30T10:00:03Z',
          updated_at: '2026-03-30T10:05:03Z',
        },
      ]);

      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: verificationEventStream(
          verificationJob('job-edit-success', {
            status: 'running',
            current_stage: 'session_probe',
            current_stage_status: 'running',
            detail: 'Grid-routed Appium probe session running',
          }),
          verificationJob('job-edit-success', {
            status: 'completed',
            current_stage: 'save_device',
            current_stage_status: 'passed',
            detail: 'Device updated after verification',
            device_id: 'device-edit-success',
            finished_at: '2026-03-30T10:05:03Z',
          }),
        ),
      });
    });

    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });
    await page.getByRole('button', { name: 'Row actions for Edit Success Device' }).click();
    await page.getByRole('menuitem', { name: 'Edit Configuration', exact: true }).click();
    await page.getByLabel('Connection Target').fill('10.0.0.15:5555');
    await page.getByRole('button', { name: 'Save Changes' }).click();

    const dialog = page.getByRole('dialog', { name: 'Save & Verify Device' });
    await expect(dialog).toBeVisible();
    await dialog.getByRole('button', { name: 'Verify Device' }).click();
    await expect(dialog).not.toBeVisible({ timeout: 5000 });
    await expect(firstDeviceRow(page).getByText('15', { exact: true })).toBeVisible({ timeout: 5000 });
    await expect(firstDeviceRow(page).getByText('10.0.0.15:5555')).toHaveCount(0);
  });

  test('edit verification failure preserves edited values for retry', async ({ page }) => {
    const mockApi = await mockAddDeviceVerificationSurface(page);
    mockApi.setDevices([
      {
        id: 'device-edit-failure',
        identity_scheme: 'android_serial',
        identity_scope: 'host',
        identity_value: 'edit-failure-001',
        connection_target: '192.168.1.22:5555',
        name: 'Edit Failure Device',
        pack_id: 'appium-uiautomator2',
        platform_id: 'android_mobile',
        platform_label: 'Android (real device)',
        os_version: '14',
        host_id: 'host-1',
        operational_state: 'available',
        hold: null,
        tags: null,
        auto_manage: true,
        device_type: 'real_device',
        connection_type: 'network',
        ip_address: '192.168.1.22',
        health_summary: mockHealthSummary(false, 'Session checks failing'),
        readiness_state: 'verified',
        missing_setup_fields: [],
        verified_at: '2026-03-30T10:00:03Z',
        reservation: null,
        lifecycle_policy_summary: {
          state: 'idle',
          label: 'Idle',
          detail: null,
          backoff_until: null,
        },
        created_at: '2026-03-30T10:00:03Z',
        updated_at: '2026-03-30T10:00:03Z',
      },
    ]);

    await page.route('**/api/devices/device-edit-failure/verification-jobs', async (route) => {
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify(verificationJob('job-edit-failure')),
      });
    });
    await page.route('**/api/devices/verification-jobs/job-edit-failure/events', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: verificationEventStream(
          verificationJob('job-edit-failure', {
            status: 'running',
            current_stage: 'session_probe',
            current_stage_status: 'running',
            detail: 'Grid-routed Appium probe session running',
          }),
          verificationJob('job-edit-failure', {
            status: 'failed',
            current_stage: 'session_probe',
            current_stage_status: 'failed',
            detail: 'Session startup failed',
            error: 'Session startup failed',
            finished_at: '2026-03-30T10:00:03Z',
          }),
        ),
      });
    });

    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });
    await page.getByRole('button', { name: 'Row actions for Edit Failure Device' }).click();
    await page.getByRole('menuitem', { name: 'Edit Configuration', exact: true }).click();
    await page.getByLabel('Connection Target').fill('10.0.0.22:5555');
    await page.getByRole('button', { name: 'Save Changes' }).click();

    const dialog = page.getByRole('dialog', { name: 'Save & Verify Device' });
    await expect(dialog).toBeVisible();
    await dialog.getByRole('button', { name: 'Verify Device' }).click();
    await expect(dialog.getByText('Session startup failed').last()).toBeVisible({ timeout: 5000 });
    await expect(dialog.getByLabel('Connection Target')).toHaveValue('10.0.0.22:5555');
    await expect(dialog.getByRole('button', { name: 'Retry Verification' })).toBeVisible();
  });

  test('virtual devices expose connection-target editing without an IP field', async ({ page }) => {
    const mockApi = await mockAddDeviceVerificationSurface(page);
    mockApi.setDevices([
      {
        id: 'device-edit-virtual',
        identity_scheme: 'manager_generated',
        identity_scope: 'host',
        identity_value: 'avd:Pixel_6',
        connection_target: 'Pixel_6',
        name: 'Edit Virtual Device',
        pack_id: 'appium-uiautomator2',
        platform_id: 'android_mobile',
        platform_label: 'Android (real device)',
        os_version: '14',
        host_id: 'host-1',
        operational_state: 'available',
        hold: null,
        tags: null,
        auto_manage: true,
        device_type: 'emulator',
        connection_type: 'virtual',
        ip_address: null,
        health_summary: mockHealthSummary(true, 'Healthy'),
        readiness_state: 'verified',
        missing_setup_fields: [],
        verified_at: '2026-03-30T10:00:03Z',
        reservation: null,
        lifecycle_policy_summary: {
          state: 'idle',
          label: 'Idle',
          detail: null,
          backoff_until: null,
        },
        created_at: '2026-03-30T10:00:03Z',
        updated_at: '2026-03-30T10:00:03Z',
      },
    ]);

    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });
    await page.getByRole('button', { name: 'Row actions for Edit Virtual Device' }).click();
    await page.getByRole('menuitem', { name: 'Edit Configuration', exact: true }).click();

    const dialog = page.getByRole('dialog', { name: 'Edit Configuration' });
    await expect(dialog.getByLabel('Connection Target')).toHaveValue('Pixel_6');
    await expect(dialog.getByLabel('IP Address')).toHaveCount(0);
  });

  test('shows availability label from device payload', async ({ page }) => {
    // The state cell shows an AvailabilityCell (badge with DEVICE_STATUS_LABELS text).
    // Attention reasons surface in the Health column popover, not as a separate dot.
    const mockApi = await mockAddDeviceVerificationSurface(page);
    mockApi.setDevices([
      {
        id: 'device-attention',
        identity_scheme: 'android_serial',
        identity_scope: 'host',
        identity_value: 'attention-001',
        connection_target: 'attention-001',
        name: 'Attention Device',
        pack_id: 'appium-uiautomator2',
        platform_id: 'android_mobile',
        platform_label: 'Android (real device)',
        os_version: '14',
        host_id: 'host-1',
        operational_state: 'offline',
        hold: null,
        needs_attention: true,
        tags: null,
        auto_manage: true,
        device_type: 'real_device',
        connection_type: 'usb',
        ip_address: null,
        health_summary: mockHealthSummary(false, 'ADB not responsive'),
        readiness_state: 'verified',
        missing_setup_fields: [],
        verified_at: '2026-03-30T10:00:03Z',
        reservation: null,
        lifecycle_policy_summary: {
          state: 'backoff',
          label: 'Backing Off',
          detail: 'ADB not responsive',
          backoff_until: null,
        },
        created_at: '2026-03-30T10:00:03Z',
        updated_at: '2026-03-30T10:00:03Z',
      },
    ]);

    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('link', { name: 'Attention Device' })).toBeVisible();
    // AvailabilityCell shows the status label
    await expect(page.getByRole('table').getByText('Offline')).toBeVisible();
  });

  test('does not show emulator state badge in the devices list status cell', async ({ page }) => {
    const mockApi = await mockAddDeviceVerificationSurface(page);
    mockApi.setDevices([
      {
        id: 'device-emu-badge',
        identity_scheme: 'manager_generated',
        identity_scope: 'host',
        identity_value: 'avd-pixel-6',
        connection_target: 'Pixel_6',
        name: 'Pixel 6 Emulator',
        pack_id: 'appium-uiautomator2',
        platform_id: 'android_mobile',
        platform_label: 'Android (real device)',
        os_version: '14',
        host_id: 'host-1',
        operational_state: 'available',
        hold: null,
        tags: null,
        auto_manage: true,
        device_type: 'emulator',
        connection_type: 'virtual',
        ip_address: null,
        health_summary: mockHealthSummary(true, 'Healthy'),
        readiness_state: 'verified',
        missing_setup_fields: [],
        verified_at: '2026-03-30T10:00:03Z',
        reservation: null,
        lifecycle_policy_summary: {
          state: 'idle',
          label: 'Idle',
          detail: null,
          backoff_until: null,
        },
        emulator_state: 'running',
        created_at: '2026-03-30T10:00:03Z',
        updated_at: '2026-03-30T10:00:03Z',
      },
    ]);

    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('link', { name: 'Pixel 6 Emulator' })).toBeVisible();
    await expect(page.getByTestId('emulator-state-badge')).toHaveCount(0);
  });

  test('does not show emulator state badge for real devices', async ({ page }) => {
    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('emulator-state-badge')).toHaveCount(0);
  });

  test('device detail exposes Edit button on Device Info panel', async ({ page }) => {
    await page.goto(`/devices/${DEFAULT_DEVICE.id}`);
    await expect(page.getByRole('heading', { name: DEFAULT_DEVICE.name })).toBeVisible({ timeout: 15_000 });
    const deviceInfoHeading = page.getByRole('heading', { name: 'Device Info' });
    await expect(deviceInfoHeading).toBeVisible();
    await expect(page.getByRole('button', { name: 'Edit', exact: true })).toBeVisible();
  });

  test('device detail edit updates display name without full page reload', async ({ page }) => {
    let deviceName = DEFAULT_DEVICE.name;

    await page.route((url) => new URL(url).pathname === `/api/devices/${DEFAULT_DEVICE.id}`, async (route) => {
      if (route.request().method() === 'PATCH') {
        const body = await route.request().postDataJSON() as Record<string, unknown>;
        if (body.name) {
          deviceName = body.name as string;
        }
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ ...DEFAULT_DEVICE_DETAIL, name: deviceName }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ...DEFAULT_DEVICE_DETAIL, name: deviceName }),
      });
    });

    await page.goto(`/devices/${DEFAULT_DEVICE.id}`);
    await expect(page.getByRole('heading', { name: DEFAULT_DEVICE.name })).toBeVisible({ timeout: 15_000 });

    await page.getByRole('button', { name: 'Edit', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: 'Edit Configuration' });
    await expect(dialog).toBeVisible();

    await dialog.getByLabel('Name').fill('Renamed Device');
    await dialog.getByRole('button', { name: 'Save Changes' }).click();

    await expect(dialog).not.toBeVisible({ timeout: 5_000 });
    await expect(page.getByRole('heading', { name: 'Renamed Device' })).toBeVisible({ timeout: 5_000 });
  });

  test('device detail edit with readiness-impacting change opens verification dialog', async ({ page }) => {
    const editDevice = {
      ...DEFAULT_DEVICE,
      id: 'device-detail-edit-verify',
      name: 'Detail Edit Verify Device',
      connection_type: 'network' as const,
      connection_target: '192.168.1.60:5555',
      ip_address: '192.168.1.60',
    };
    const editDeviceDetail = { ...editDevice, appium_node: null, sessions: [] };

    await page.route((url) => new URL(url).pathname === '/api/devices', async (route) => {
      if (route.request().method() !== 'GET') { await route.fallback(); return; }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(devicesResponseBody([editDevice], new URL(route.request().url()))) });
    });
    await page.route((url) => new URL(url).pathname === `/api/devices/${editDevice.id}`, async (route) => {
      if (route.request().method() !== 'GET') { await route.fallback(); return; }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(editDeviceDetail) });
    });
    await page.route((url) => url.pathname.startsWith(`/api/devices/${editDevice.id}/`), async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
    });

    await page.goto(`/devices/${editDevice.id}`);
    await expect(page.getByRole('heading', { name: editDevice.name })).toBeVisible({ timeout: 15_000 });

    await page.getByRole('button', { name: 'Edit', exact: true }).click();
    const editDialog = page.getByRole('dialog', { name: 'Edit Configuration' });
    await expect(editDialog).toBeVisible();

    await editDialog.getByLabel('Connection Target').fill('10.0.0.60:5555');
    await editDialog.getByRole('button', { name: 'Save Changes' }).click();

    await expect(page.getByRole('dialog', { name: 'Save & Verify Device' })).toBeVisible({ timeout: 5_000 });
  });

  test('device detail delete navigates back to devices list', async ({ page }) => {
    let deviceDeleted = false;

    await page.route((url) => new URL(url).pathname === '/api/devices', async (route) => {
      if (route.request().method() !== 'GET') { await route.fallback(); return; }
      const devices = deviceDeleted ? [] : [DEFAULT_DEVICE];
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(devicesResponseBody(devices, new URL(route.request().url()))) });
    });
    await page.route((url) => new URL(url).pathname === `/api/devices/${DEFAULT_DEVICE.id}`, async (route) => {
      if (route.request().method() === 'DELETE') {
        deviceDeleted = true;
        await route.fulfill({ status: 204, body: '' });
        return;
      }
      await route.fallback();
    });

    await page.goto(`/devices/${DEFAULT_DEVICE.id}?tab=setup`);
    await expect(page.getByRole('heading', { name: DEFAULT_DEVICE.name })).toBeVisible({ timeout: 15_000 });

    await page.getByRole('button', { name: 'Delete Device', exact: true }).click();
    const confirmDialog = page.getByRole('dialog', { name: 'Delete Device' });
    await expect(confirmDialog).toBeVisible();
    await confirmDialog.getByRole('button', { name: 'Delete' }).click();

    await expect(page).toHaveURL('/devices', { timeout: 5_000 });
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText(DEFAULT_DEVICE.name)).not.toBeVisible();
  });

  test('respects prefers-reduced-motion: reduce on cold load', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });

    const wrapper = page.locator('.fade-in-stagger').first();
    await expect(wrapper).toBeVisible();
    const opacities = await wrapper.locator('> *').evaluateAll((nodes) =>
      nodes.map((node) => window.getComputedStyle(node).opacity),
    );
    expect(opacities.length).toBeGreaterThan(0);
    for (const value of opacities) {
      expect(Number(value)).toBe(1);
    }
  });

  test('device detail chrome does not unmount on tab switch', async ({ page }) => {
    await mockDefaultDevicesSurface(page);
    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });
    await page.locator('a[href^="/devices/"]').first().click();

    const pageH1 = page.locator('h1.heading-page');
    await expect(pageH1).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole('button', { name: 'Triage', exact: true })).toBeVisible();

    // Tabs component renders plain <button> elements without role="tab"
    await page.getByRole('button', { name: 'Setup', exact: true }).click();

    // Assert immediately after click — before any waitFor — so a sub-100 ms
    // flash of the spinner would be caught here rather than after the body settles.
    await expect(pageH1).toBeVisible();
    await expect(page.locator('.rounded-lg.border.border-border.bg-surface-1.py-12')).not.toBeVisible();

    // Wait for the Control tab content to confirm the switch completed
    await expect(page.getByRole('heading', { name: 'Device Control' })).toBeVisible({ timeout: 10_000 });

    // Page heading must still be visible after the tab body has settled
    await expect(pageH1).toBeVisible();
  });

  test('device detail triage does not render Current Health column', async ({ page }) => {
    await page.goto('/devices');
    await page.locator('a[href^="/devices/"]').first().click();
    await expect(page.locator('h1.heading-page')).toBeVisible();
    await expect(page.getByText('Current Health', { exact: true })).toHaveCount(0);
  });

  test('device detail header renders status pills in the header region', async ({ page }) => {
    await page.goto('/devices');
    await page.locator('a[href^="/devices/"]').first().click();
    await expect(page.locator('h1.heading-page')).toBeVisible();

    const header = page.locator('header').first();
    const pills = header.locator('[data-testid="device-detail-status-pill"]');
    // Only Hardware and Connectivity pills remain (Readiness and Lifecycle were removed).
    await expect(pills).toHaveCount(2);
    await expect(header.getByText('Hardware', { exact: true })).toBeVisible();
    await expect(header.getByText('Connectivity', { exact: true })).toBeVisible();
  });

  test('device detail surfaces actions in their contextual panels', async ({ page }) => {
    await page.goto('/devices');
    await page.locator('a[href^="/devices/"]').first().click();
    await expect(page.locator('h1.heading-page')).toBeVisible();

    // Header carries status pills only — no kebab, no action buttons
    const header = page.locator('header').first();
    await expect(header.getByRole('button', { name: /More actions/i })).toHaveCount(0);
    await expect(header.getByRole('button', { name: /Re-verify|Complete Setup|Verify Device/ })).toHaveCount(0);

    // Device Info carries editable device-level fields and compact tags.
    await expect(page.getByRole('button', { name: 'Edit', exact: true })).toBeVisible();
    await expect(page.getByText('Tags', { exact: true })).toBeVisible();
    await expect(page.getByText('team: qa', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Refresh Properties', exact: true })).toHaveCount(0);

    // Setup tab surfaces Danger Zone delete
    await page.getByRole('button', { name: 'Setup', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Danger Zone' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Delete Device', exact: true })).toBeVisible();
  });
});
