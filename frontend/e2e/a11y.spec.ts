import type { Page } from '@playwright/test';
import { test, expect } from './helpers/fixtures';
import { fulfillJson } from './helpers/routes';
import AxeBuilder from '@axe-core/playwright';

const DEVICE = {
  id: 'device-1',
  pack_id: 'appium-uiautomator2',
  platform_id: 'android_mobile',
  platform_label: 'Android',
  identity_scheme: 'android_serial',
  identity_scope: 'host',
  identity_value: 'device-001',
  connection_target: '192.168.1.50:5555',
  name: 'Pixel 8',
  os_version: '14',
  host_id: 'host-1',
  operational_state: 'available',
  hold: null,
  tags: { team: 'qa' },
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
  lifecycle_policy_summary: { state: 'idle', label: 'Idle', detail: null, backoff_until: null },
  health_summary: { device: { status: 'ok', detail: null, checked_at: null }, node: { status: 'ok', detail: 'running', checked_at: null }, viability: { status: 'ok', detail: 'passed', checked_at: null }, overall: 'ok' },
  emulator_state: null,
  created_at: '2026-03-30T10:00:03Z',
  updated_at: '2026-03-30T10:00:03Z',
};

const HOST = {
  id: 'host-1',
  hostname: 'lab-mac-mini',
  ip: '10.0.0.10',
  os_type: 'macos',
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
};

const SESSION = {
  id: 'session-1',
  session_id: 'abcd1234efgh5678',
  device_id: 'device-1',
  device_name: 'Pixel 8',
  device_pack_id: 'appium-uiautomator2',
  device_platform_id: 'android_mobile',
  device_platform_label: 'Android',
  test_name: 'test_checkout',
  started_at: '2026-03-30T09:00:00Z',
  ended_at: '2026-03-30T09:12:00Z',
  status: 'passed',
  requested_capabilities: null,
  error_type: null,
  error_message: null,
  run_id: null,
};

const RUN = {
  id: 'run-1',
  name: 'ci-smoke',
  state: 'active',
  requirements: [{ pack_id: 'appium-uiautomator2', platform_id: 'android_mobile', count: 1 }],
  ttl_minutes: 60,
  heartbeat_timeout_sec: 120,
  reserved_devices: [],
  error: null,
  created_at: '2026-03-30T10:00:00Z',
  started_at: '2026-03-30T10:01:00Z',
  completed_at: null,
  created_by: 'github/actions',
  last_heartbeat: '2026-03-30T10:05:00Z',
  session_counts: { passed: 1, failed: 0, error: 0, running: 0, total: 1 },
};

const EVENT = {
  id: 'evt-1',
  type: 'device.operational_state_changed',
  severity: 'info',
  timestamp: '2026-03-30T10:00:00Z',
  data: { device_id: 'device-1' },
};

const SETTINGS_GROUPED = [
  {
    category: 'general',
    display_name: 'General',
    settings: [
      {
        key: 'general.heartbeat_interval_sec',
        category: 'general',
        type: 'int',
        description: 'Heartbeat interval for hosts in seconds.',
        default_value: 15,
        value: 15,
        validation: { min: 5, max: 120 },
      },
    ],
  },
];

const THEMES: Array<{ name: string; setup: (page: Page) => Promise<void> }> = [
  { name: 'light', setup: async () => {} },
  {
    name: 'dark',
    setup: async (page) => {
      await page.addInitScript(() => {
        window.localStorage.setItem('gridfleet.theme', 'dark');
      });
    },
  },
];

test.describe('Accessibility', () => {
  test.beforeEach(async ({ page }) => {
    await page.route((url) => new URL(url).pathname === '/api/devices', async (route) => {
      await fulfillJson(route, [DEVICE]);
    });
    await page.route((url) => new URL(url).pathname === '/api/hosts', async (route) => {
      await fulfillJson(route, [HOST]);
    });
    await page.route((url) => {
      const path = new URL(url).pathname;
      return path === '/api/sessions';
    }, async (route) => {
      await fulfillJson(route, {
        items: [SESSION],
        total: 1,
        limit: 50,
        offset: 0,
        next_cursor: null,
        prev_cursor: null,
      });
    });
    await page.route((url) => new URL(url).pathname === '/api/runs', async (route) => {
      await fulfillJson(route, {
        items: [RUN],
        total: 1,
        limit: 50,
        offset: 0,
        next_cursor: null,
        prev_cursor: null,
      });
    });
    await page.route((url) => new URL(url).pathname === '/api/events/stream', async (route) => {
      await fulfillJson(route, {
        items: [EVENT],
        total: 1,
        limit: 50,
        offset: 0,
        next_cursor: null,
        prev_cursor: null,
      });
    });
    await page.route((url) => new URL(url).pathname === '/api/events/catalog', async (route) => {
      await fulfillJson(route, { events: [{ name: 'device.state_changed', description: 'Device state changed', severity: 'info' }] });
    });
    await page.route((url) => new URL(url).pathname === '/api/notifications', async (route) => {
      await fulfillJson(route, {
        items: [EVENT],
        total: 1,
        limit: 50,
        offset: 0,
        next_cursor: null,
        prev_cursor: null,
      });
    });
    await page.route((url) => new URL(url).pathname === '/api/settings', async (route) => {
      await fulfillJson(route, SETTINGS_GROUPED);
    });
    await page.route((url) => {
      const path = new URL(url).pathname;
      return /^\/api\/hosts\/[^/]+\/intake-candidates$/.test(path);
    }, async (route) => {
      await fulfillJson(route, []);
    });
    await page.route((url) => {
      const path = new URL(url).pathname;
      return /^\/api\/hosts\/[^/]+\/driver-packs$/.test(path);
    }, async (route) => {
      await fulfillJson(route, []);
    });

    // Device groups
    await page.route((url) => new URL(url).pathname === '/api/device-groups', async (route) => {
      await fulfillJson(route, [
        {
          id: 'group-1',
          name: 'QA Devices',
          description: 'Shared devices for QA workflows',
          group_type: 'static',
          device_count: 1,
          filters: null,
          created_at: '2026-04-04T10:00:00Z',
          updated_at: '2026-04-04T10:00:00Z',
        },
      ]);
    });

    // Device detail
    await page.route((url) => new URL(url).pathname === '/api/devices/device-1', async (route) => {
      await fulfillJson(route, {
        ...DEVICE,
        appium_node: {
          id: 'node-1',
          port: 4723,
          grid_url: 'http://127.0.0.1:4444',
          pid: 4242,
          container_id: null,
          state: 'running',
          started_at: '2026-03-30T10:00:03Z',
        },
        sessions: [],
      });
    });
    await page.route((url) => new URL(url).pathname === '/api/devices/device-1/health', async (route) => {
      await fulfillJson(route, {
        platform: 'android_mobile',
        healthy: true,
        node: { running: true, port: 4723, state: 'running' },
        device_checks: { adb: { status: 'ok' } },
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
          deferred_stop: false,
          deferred_stop_reason: null,
          deferred_stop_since: null,
          excluded_from_run: false,
          excluded_run_id: null,
          excluded_run_name: null,
          excluded_at: null,
          will_auto_rejoin_run: false,
          recovery_suppressed_reason: null,
          backoff_until: null,
          recovery_state: 'idle',
        },
      });
    });
    await page.route((url) => new URL(url).pathname === '/api/devices/device-1/session-outcome-heatmap', async (route) => {
      await fulfillJson(route, [
        { timestamp: '2026-03-29T12:00:00Z', status: 'passed' },
        { timestamp: '2026-03-30T12:00:00Z', status: 'failed' },
      ]);
    });
    await page.route((url) => new URL(url).pathname === '/api/devices/device-1/config', async (route) => {
      await fulfillJson(route, { automationName: 'UiAutomator2' });
    });
    await page.route((url) => new URL(url).pathname === '/api/devices/device-1/config/history', async (route) => {
      await fulfillJson(route, []);
    });
    await page.route((url) => new URL(url).pathname === '/api/devices/device-1/logs', async (route) => {
      await fulfillJson(route, { lines: ['2026-03-30 10:00:03 INFO AppiumServer started'], count: 1 });
    });
    await page.route((url) => new URL(url).pathname === '/api/devices/device-1/capabilities', async (route) => {
      await fulfillJson(route, { platformName: 'Android', 'appium:udid': 'device-001' });
    });
    await page.route((url) => new URL(url).pathname === '/api/devices/device-1/test_data', async (route) => {
      await fulfillJson(route, { test_name: null, test_suite: null, test_result: null });
    });

    // Host detail
    await page.route((url) => new URL(url).pathname === '/api/hosts/host-1/tools/status', async (route) => {
      await fulfillJson(route, { tools: [] });
    });
    await page.route((url) => new URL(url).pathname === '/api/hosts/host-1', async (route) => {
      await fulfillJson(route, { ...HOST, devices: [DEVICE] });
    });
    await page.route((url) => new URL(url).pathname === '/api/hosts/host-1/diagnostics', async (route) => {
      await fulfillJson(route, {
        host_id: 'host-1',
        circuit_breaker: {
          status: 'closed',
          consecutive_failures: 0,
          cooldown_seconds: 0,
          retry_after_seconds: 0,
          probe_in_flight: false,
          last_error: null,
        },
        appium_processes: { reported_at: '2026-03-30T10:00:00Z', running_nodes: [] },
        recent_recovery_events: [],
      });
    });
    await page.route((url) => new URL(url).pathname === '/api/hosts/host-1/resource-telemetry', async (route) => {
      await fulfillJson(route, {
        samples: [],
        latest_recorded_at: null,
        window_start: '2026-03-30T09:00:00Z',
        window_end: '2026-03-30T10:00:00Z',
        bucket_minutes: 5,
      });
    });
    await page.route((url) => new URL(url).pathname === '/api/hosts/host-1/events', async (route) => {
      await fulfillJson(route, { events: [], total: 0, limit: 50, offset: 0 });
    });

    // Run detail
    await page.route((url) => new URL(url).pathname === '/api/runs/run-1', async (route) => {
      await fulfillJson(route, RUN);
    });

    // Group detail
    await page.route((url) => new URL(url).pathname === '/api/device-groups/group-1', async (route) => {
      await fulfillJson(route, {
        id: 'group-1',
        name: 'QA Devices',
        description: 'Shared devices for QA workflows',
        group_type: 'static',
        device_count: 1,
        devices: [DEVICE],
        filters: null,
        created_at: '2026-04-04T10:00:00Z',
        updated_at: '2026-04-04T10:00:00Z',
      });
    });

    // Driver-pack releases and hosts (detail page)
    await page.route((url) => /^\/api\/driver-packs\/[^/]+\/releases$/.test(new URL(url).pathname), async (route) => {
      await fulfillJson(route, { releases: [], total: 0 });
    });
    await page.route((url) => /^\/api\/driver-packs\/[^/]+\/hosts$/.test(new URL(url).pathname), async (route) => {
      await fulfillJson(route, { hosts: [] });
    });
  });

  const ROUTES: Array<{ path: string; heading: string }> = [
    { path: '/', heading: 'Dashboard' },
    { path: '/devices', heading: 'Devices' },
    { path: '/sessions', heading: 'Sessions' },
    { path: '/runs', heading: 'Test Runs' },
    { path: '/analytics', heading: 'Analytics' },
    { path: '/notifications', heading: 'Notifications' },
    { path: '/settings', heading: 'Settings' },
    { path: '/hosts', heading: 'Hosts' },
    { path: '/groups', heading: 'Device Groups' },
    { path: '/drivers', heading: 'Driver Packs' },
    { path: '/devices/device-1', heading: 'Pixel 8' },
    { path: '/hosts/host-1', heading: 'lab-mac-mini' },
    { path: '/runs/run-1', heading: 'ci-smoke' },
    { path: '/groups/group-1', heading: 'QA Devices' },
    { path: '/drivers/appium-uiautomator2', heading: 'Appium UiAutomator2' },
  ];

  for (const theme of THEMES) {
    for (const { path, heading } of ROUTES) {
      test(`[${theme.name}] no a11y violations on ${path}`, async ({ page }) => {
        await page.emulateMedia({ reducedMotion: 'reduce' });
        await theme.setup(page);
        await page.goto(path);
        await expect(page.getByRole('heading', { name: heading, level: 1 })).toBeVisible({ timeout: 15_000 });

        const results = await new AxeBuilder({ page }).analyze();

        expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
      });
    }
  }
});

test.describe('Accessibility – public pages', () => {
  const PUBLIC_ROUTES: Array<{ path: string; heading: string }> = [
    { path: '/login', heading: 'GridFleet' },
    { path: '/no-such-page', heading: '404' },
  ];

  for (const theme of THEMES) {
    for (const { path, heading } of PUBLIC_ROUTES) {
      test(`[${theme.name}] no a11y violations on ${path}`, async ({ page }) => {
        await page.emulateMedia({ reducedMotion: 'reduce' });
        await theme.setup(page);
        await page.goto(path);
        await expect(page.getByRole('heading', { name: heading, level: 1 })).toBeVisible({ timeout: 15_000 });

        const results = await new AxeBuilder({ page }).analyze();

        expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
      });
    }
  }
});
