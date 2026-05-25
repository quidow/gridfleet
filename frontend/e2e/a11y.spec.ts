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
  health_summary: { healthy: true, summary: 'Healthy', last_checked_at: '2026-03-30T10:00:03Z' },
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
  requested_pack_id: null,
  requested_platform_id: null,
  requested_device_type: null,
  requested_connection_type: null,
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
    await page.route((url) => new URL(url).pathname === '/api/settings', async (route) => {
      await fulfillJson(route, SETTINGS_GROUPED);
    });
    await page.route((url) => new URL(url).pathname === '/api/webhooks', async (route) => {
      await fulfillJson(route, []);
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
  });

  const ROUTES: Array<{ path: string; heading: string }> = [
    { path: '/', heading: 'Dashboard' },
    { path: '/devices', heading: 'Devices' },
    { path: '/sessions', heading: 'Sessions' },
    { path: '/runs', heading: 'Test Runs' },
    { path: '/analytics', heading: 'Analytics' },
    { path: '/notifications', heading: 'Notifications' },
    { path: '/settings', heading: 'Settings' },
  ];

  for (const { path, heading } of ROUTES) {
    test(`has no critical automated a11y violations on ${path}`, async ({ page }) => {
      await page.goto(path);
      await expect(page.getByRole('heading', { name: heading, level: 1 })).toBeVisible({ timeout: 15_000 });

      const results = await new AxeBuilder({ page })
        .disableRules(['color-contrast'])
        .analyze();

      expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
    });
  }
});
