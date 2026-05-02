import { type Page, type Route } from '@playwright/test';
import { test, expect } from './helpers/fixtures';

async function fulfillJson(route: Route, body: unknown) {
  await route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });
}

const DEFAULT_HOSTS = [
  {
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
  },
];

const DEFAULT_DEVICES = [
  {
    id: 'device-1',
    identity_scheme: 'simulator_udid',
    identity_scope: 'host',
    identity_value: 'ios-sim-001',
    connection_target: 'sim://ios-sim-001',
    name: 'iPhone 15',
    pack_id: 'appium-xcuitest',
    platform_id: 'ios',
    platform_label: 'iOS Simulator',
    os_version: '17.4',
    host_id: 'host-1',
    status: 'available',
    tags: null,
    auto_manage: true,
    device_type: 'simulator',
    connection_type: 'virtual',
    ip_address: null,
    readiness_state: 'verified',
    missing_setup_fields: [],
    verified_at: '2026-03-30T10:00:00Z',
    reservation: null,
    lifecycle_policy_summary: { state: 'idle', label: 'Idle', detail: null, backoff_until: null },
    health_summary: { healthy: true, summary: 'Healthy', last_checked_at: '2026-03-30T10:00:00Z' },
    created_at: '2026-03-30T10:00:00Z',
    updated_at: '2026-03-30T10:00:00Z',
  },
];

const DEFAULT_INTAKE_CANDIDATES: Record<string, unknown[]> = {
  'host-1': [],
};

async function mockDefaultHostsSurface(page: Page) {
  await page.route((url) => new URL(url).pathname === '/api/events', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: '',
    });
  });

  await page.route((url) => new URL(url).pathname === '/api/hosts', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    await fulfillJson(route, DEFAULT_HOSTS);
  });

  await page.route((url) => new URL(url).pathname === '/api/devices', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    await fulfillJson(route, DEFAULT_DEVICES);
  });

  await page.route((url) => new URL(url).pathname === '/api/hosts/host-1', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    await fulfillJson(route, {
      ...DEFAULT_HOSTS[0],
      devices: DEFAULT_DEVICES,
    });
  });

  await page.route((url) => /\/api\/hosts\/[^/]+\/intake-candidates$/.test(new URL(url).pathname), async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    const hostId = new URL(route.request().url()).pathname.split('/')[3];
    await fulfillJson(route, DEFAULT_INTAKE_CANDIDATES[hostId] ?? []);
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

  await page.route((url) => new URL(url).pathname === '/api/hosts/host-1/diagnostics', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    await fulfillJson(route, {
      host_id: 'host-1',
      circuit_breaker: {
        status: 'closed',
        consecutive_failures: 0,
        cooldown_seconds: 30,
        retry_after_seconds: null,
        probe_in_flight: false,
        last_error: null,
      },
      appium_processes: {
        reported_at: '2026-03-30T10:01:00Z',
        running_nodes: [],
      },
      recent_recovery_events: [],
    });
  });

  await page.route((url) => new URL(url).pathname === '/api/hosts/host-1/resource-telemetry', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    await fulfillJson(route, {
      samples: [],
      latest_recorded_at: null,
      window_start: '2026-03-30T09:00:00Z',
      window_end: '2026-03-30T10:00:00Z',
      bucket_minutes: 5,
    });
  });
}

test.describe('Hosts page', () => {
  test.beforeEach(async ({ page }) => {
    await mockDefaultHostsSurface(page);
  });

  test('loads and shows host table', async ({ page }) => {
    await page.goto('/hosts');
    await expect(page.getByRole('heading', { name: 'Hosts', exact: true })).toBeVisible({ timeout: 15_000 });

    const headers = ['Hostname', 'IP', 'OS', 'Status', 'Agent Version', 'Devices', 'Last Heartbeat'];
    for (const h of headers) {
      await expect(page.getByRole('button', { name: h, exact: true })).toBeVisible();
    }
    await expect(page.getByText(/Showing \d+ hosts/)).toBeVisible();
  });

  test('shows registered hosts', async ({ page }) => {
    await page.goto('/hosts');
    await expect(page.getByRole('heading', { name: 'Hosts', exact: true })).toBeVisible({ timeout: 15_000 });

    const rows = page.locator('tbody tr');
    await expect(rows).not.toHaveCount(0);
  });

  test('Add Host button opens modal', async ({ page }) => {
    await page.goto('/hosts');
    await expect(page.getByRole('heading', { name: 'Hosts', exact: true })).toBeVisible({ timeout: 15_000 });

    await page.getByRole('button', { name: 'Add Host' }).click();
    const modal = page.getByRole('heading', { name: 'Add Host' });
    await expect(modal).toBeVisible();
    const form = page.locator('form');
    await expect(form.getByText('Hostname', { exact: true })).toBeVisible();
    await expect(form.getByText('IP Address')).toBeVisible();
    await expect(form.getByText('OS Type')).toBeVisible();
    await expect(form.getByText('Agent Port')).toBeVisible();

    await page.getByRole('button', { name: 'Cancel' }).click();
    await expect(modal).not.toBeVisible();
  });

  test('host name links to detail page', async ({ page }) => {
    await page.goto('/hosts');
    await expect(page.getByRole('heading', { name: 'Hosts', exact: true })).toBeVisible({ timeout: 15_000 });

    const firstHostLink = page.locator('tbody tr').first().getByRole('link').first();
    const name = await firstHostLink.textContent();
    await firstHostLink.click();

    await expect(page.getByRole('heading', { name: name! })).toBeVisible({ timeout: 10_000 });
  });

  test('sortable host headers can be clicked', async ({ page }) => {
    await page.goto('/hosts');
    await expect(page.getByRole('heading', { name: 'Hosts', exact: true })).toBeVisible({ timeout: 15_000 });

    await page.getByRole('button', { name: 'Devices', exact: true }).click();
    await page.getByRole('button', { name: 'Hostname', exact: true }).click();
    await expect(page.locator('tbody tr').first()).toBeVisible();
  });

  test('renders summary telemetry and filter shortcuts', async ({ page }) => {
    await page.route((url) => new URL(url).pathname === '/api/hosts', async (route) => {
      if (route.request().method() !== 'GET') {
        await route.fallback();
        return;
      }

      await fulfillJson(route, [
        {
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
        },
        {
          id: 'host-2',
          hostname: 'lab-linux',
          ip: '10.0.0.11',
          os_type: 'linux',
          agent_port: 5100,
          status: 'offline',
          agent_version: '0.9.0',
          required_agent_version: '1.0.0',
          recommended_agent_version: '1.0.0',
          agent_update_available: false,
          agent_version_status: 'outdated',
          capabilities: null,
          last_heartbeat: null,
          created_at: '2026-03-30T10:00:00Z',
        },
        {
          id: 'host-3',
          hostname: 'lab-firetv',
          ip: '10.0.0.12',
          os_type: 'linux',
          agent_port: 5100,
          status: 'online',
          agent_version: '0.8.0',
          required_agent_version: '1.0.0',
          recommended_agent_version: '1.0.0',
          agent_update_available: false,
          agent_version_status: 'outdated',
          capabilities: null,
          last_heartbeat: '2026-03-30T10:02:00Z',
          created_at: '2026-03-30T10:00:00Z',
        },
      ]);
    });

    await page.route((url) => new URL(url).pathname === '/api/devices', async (route) => {
      if (route.request().method() !== 'GET') {
        await route.fallback();
        return;
      }

      await fulfillJson(route, [
        ...DEFAULT_DEVICES,
        {
          ...DEFAULT_DEVICES[0],
          id: 'device-2',
          name: 'Fire TV Stick',
          host_id: 'host-2',
          pack_id: 'appium-firetv',
          platform_id: 'firetv_network',
          platform_label: 'Fire TV Network',
          identity_scheme: 'android_serial',
          identity_scope: 'host',
          identity_value: 'firetv-001',
          connection_target: 'firetv-001',
        },
      ]);
    });

    await page.route((url) => /\/api\/hosts\/[^/]+\/intake-candidates$/.test(new URL(url).pathname), async (route) => {
      if (route.request().method() !== 'GET') {
        await route.fallback();
        return;
      }

      const hostId = new URL(route.request().url()).pathname.split('/')[3];
      if (hostId === 'host-1') {
        await fulfillJson(route, [
          {
            identity_scheme: 'android_serial',
            identity_scope: 'host',
            identity_value: 'candidate-1',
            connection_target: 'candidate-1',
            name: 'Pixel 9',
            pack_id: 'appium-uiautomator2',
            platform_id: 'android_mobile',
            platform_label: 'Android (real device)',
            os_version: '15',
            manufacturer: 'Google',
            model: 'Pixel 9',
            detected_properties: null,
            device_type: 'real_device',
            connection_type: 'usb',
            ip_address: null,
            already_registered: false,
            registered_device_id: null,
          },
          {
            identity_scheme: 'android_serial',
            identity_scope: 'host',
            identity_value: 'candidate-registered',
            connection_target: 'candidate-registered',
            name: 'Known Device',
            pack_id: 'appium-uiautomator2',
            platform_id: 'android_mobile',
            platform_label: 'Android (real device)',
            os_version: '15',
            manufacturer: 'Google',
            model: 'Pixel 8',
            detected_properties: null,
            device_type: 'real_device',
            connection_type: 'usb',
            ip_address: null,
            already_registered: true,
            registered_device_id: 'device-1',
          },
        ]);
        return;
      }

      if (hostId === 'host-3') {
        await fulfillJson(route, []);
        return;
      }

      await fulfillJson(route, []);
    });

    await page.goto('/hosts');
    await expect(page.getByRole('link', { name: 'Offline 1' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Stale agents 2' })).toBeVisible();

    await page.getByRole('link', { name: 'Offline 1' }).click();
    await expect(page).toHaveURL(/status=offline/);
    await expect(page.locator('tbody tr')).toHaveCount(1);
    await expect(page.getByRole('link', { name: 'lab-linux' })).toBeVisible();

    await page.getByRole('link', { name: 'Total 3' }).click();
    await expect(page).toHaveURL(/\/hosts$/);

    await page.getByRole('link', { name: 'Stale agents 2' }).click();
    await expect(page).toHaveURL(/agent_version_status=outdated/);
    await expect(page.locator('tbody tr')).toHaveCount(2);
    await expect(page.getByRole('link', { name: 'lab-linux' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'lab-firetv' })).toBeVisible();
  });

  test('shares agent version trust presentation between hosts list and host detail', async ({ page }) => {
    await page.route((url) => new URL(url).pathname === '/api/events', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: '',
      });
    });

    await page.route((url) => new URL(url).pathname === '/api/devices', async (route) => {
      if (route.request().method() !== 'GET') {
        await route.fallback();
        return;
      }
      await fulfillJson(route, []);
    });

    await page.route((url) => /\/api\/hosts\/[^/]+\/intake-candidates$/.test(new URL(url).pathname), async (route) => {
      if (route.request().method() !== 'GET') {
        await route.fallback();
        return;
      }
      await fulfillJson(route, []);
    });

    await page.route((url) => new URL(url).pathname === '/api/hosts', async (route) => {
      if (route.request().method() !== 'GET') {
        await route.fallback();
        return;
      }

      await fulfillJson(route, [
        {
          id: 'host-1',
          hostname: 'lab-mac-mini',
          ip: '10.0.0.10',
          os_type: 'macos',
          agent_port: 5100,
          status: 'online',
          agent_version: '0.0.9',
          required_agent_version: '0.1.0',
          recommended_agent_version: '0.1.0',
          agent_update_available: false,
          agent_version_status: 'outdated',
          capabilities: null,
          last_heartbeat: '2026-03-30T10:00:00Z',
          created_at: '2026-03-30T10:00:00Z',
        },
        {
          id: 'host-2',
          hostname: 'lab-linux',
          ip: '10.0.0.11',
          os_type: 'linux',
          agent_port: 5100,
          status: 'offline',
          agent_version: 'not-semver',
          required_agent_version: '0.1.0',
          recommended_agent_version: '0.1.0',
          agent_update_available: false,
          agent_version_status: 'unknown',
          capabilities: null,
          last_heartbeat: null,
          created_at: '2026-03-30T10:00:00Z',
        },
      ]);
    });

    await page.route((url) => new URL(url).pathname === '/api/hosts/host-1', async (route) => {
      if (route.request().method() !== 'GET') {
        await route.fallback();
        return;
      }

      await fulfillJson(route, {
        id: 'host-1',
        hostname: 'lab-mac-mini',
        ip: '10.0.0.10',
        os_type: 'macos',
        agent_port: 5100,
        status: 'online',
        agent_version: '0.0.9',
        required_agent_version: '0.1.0',
        recommended_agent_version: '0.1.0',
        agent_update_available: false,
        agent_version_status: 'outdated',
        capabilities: { platforms: ['ios'], tools: { appium: '3.0.0' } },
        last_heartbeat: '2026-03-30T10:00:00Z',
        created_at: '2026-03-30T10:00:00Z',
        devices: DEFAULT_DEVICES,
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

    await page.route((url) => new URL(url).pathname === '/api/hosts/host-1/diagnostics', async (route) => {
      if (route.request().method() !== 'GET') {
        await route.fallback();
        return;
      }

      await fulfillJson(route, {
        host_id: 'host-1',
        circuit_breaker: {
          status: 'open',
          consecutive_failures: 5,
          cooldown_seconds: 30,
          retry_after_seconds: 12,
          probe_in_flight: false,
          last_error: 'HTTP 503',
        },
        appium_processes: {
          reported_at: '2026-03-30T10:01:00Z',
          running_nodes: [
            {
              port: 4723,
              pid: 2222,
              connection_target: 'dev-device-1',
              platform_id: 'ios',
              managed: true,
              node_id: 'node-1',
              node_state: 'running',
              device_id: 'device-1',
              device_name: 'iPhone 15',
            },
          ],
        },
        recent_recovery_events: [
          {
            id: 'event-1',
            device_id: 'device-1',
            device_name: 'iPhone 15',
            event_type: 'node_restart',
            process: 'grid_relay',
            kind: 'restart_succeeded',
            sequence: 3,
            port: 4723,
            pid: 2222,
            attempt: 1,
            delay_sec: 1,
            exit_code: 1,
            will_restart: false,
            occurred_at: '2026-03-30T10:00:59Z',
            recorded_at: '2026-03-30T10:01:00Z',
          },
        ],
      });
    });

    await page.route((url) => new URL(url).pathname === '/api/hosts/host-1/resource-telemetry', async (route) => {
      if (route.request().method() !== 'GET') {
        await route.fallback();
        return;
      }

      await fulfillJson(route, {
        samples: [
          {
            timestamp: '2026-03-30T09:50:00Z',
            cpu_percent: 42.5,
            memory_used_mb: 12000,
            memory_total_mb: 32000,
            disk_used_gb: 200.0,
            disk_total_gb: 500.0,
            disk_percent: 40.0,
          },
        ],
        latest_recorded_at: '2026-03-30T09:50:00Z',
        window_start: '2026-03-30T09:00:00Z',
        window_end: '2026-03-30T10:00:00Z',
        bucket_minutes: 5,
      });
    });

    await page.goto('/hosts');
    await expect(page.getByText('Outdated')).toBeVisible();
    await expect(page.getByText('Unknown', { exact: true })).toBeVisible();

    await page.getByRole('link', { name: 'lab-mac-mini' }).click();
    // Overview tab is default — agent version notice and actions are here
    await expect(page.getByText('Agent update recommended')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Discover Devices' })).toBeVisible();

    // Diagnostics tab — circuit breaker and recovery events
    await page.getByRole('button', { name: 'Diagnostics', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Diagnostics' })).toBeVisible();
    await expect(page.getByText('Host Resource Telemetry')).toBeVisible();
    await expect(page.getByText('Circuit Breaker')).toBeVisible();
    await expect(page.getByText('HTTP 503')).toBeVisible();
    await expect(page.getByText('Grid Relay')).toBeVisible();
    await expect(page.getByText('Restart Succeeded')).toBeVisible();

    // Devices tab — device links
    await page.getByRole('button', { name: 'Devices', exact: true }).click();
    await expect(page.getByRole('link', { name: 'iPhone 15' }).first()).toBeVisible();
  });

  test('host detail tab navigation is URL-addressable', async ({ page }) => {
    await mockDefaultHostsSurface(page);
    await page.goto('/hosts/host-1?tab=drivers');
    await expect(page.getByRole('heading', { name: 'lab-mac-mini' })).toBeVisible({ timeout: 15_000 });
    // Deep-linked Drivers tab should show drivers table area
    await expect(page.getByText('Appium Drivers')).toBeVisible();

    // Switch to Plugins tab
    await page.getByRole('button', { name: 'Plugins', exact: true }).click();
    await expect(page).toHaveURL(/tab=plugins/);

    // Unknown tab falls back to Overview
    await page.goto('/hosts/host-1?tab=bogus');
    await expect(page.getByRole('heading', { name: 'lab-mac-mini' })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText('Host Info')).toBeVisible();
  });

  test('diagnostics tab shows host resource telemetry charts', async ({ page }) => {
    await page.route((url) => new URL(url).pathname === '/api/hosts/host-1/resource-telemetry', async (route) => {
      if (route.request().method() !== 'GET') {
        await route.fallback();
        return;
      }

      await fulfillJson(route, {
        samples: [
          {
            timestamp: '2026-03-30T09:50:00Z',
            cpu_percent: 42.5,
            memory_used_mb: 12000,
            memory_total_mb: 32000,
            disk_used_gb: 200.0,
            disk_total_gb: 500.0,
            disk_percent: 40.0,
          },
          {
            timestamp: '2026-03-30T09:55:00Z',
            cpu_percent: 55.0,
            memory_used_mb: 14000,
            memory_total_mb: 32000,
            disk_used_gb: 205.0,
            disk_total_gb: 500.0,
            disk_percent: 41.0,
          },
          {
            timestamp: '2026-03-30T10:00:00Z',
            cpu_percent: 61.5,
            memory_used_mb: 15000,
            memory_total_mb: 32000,
            disk_used_gb: 210.0,
            disk_total_gb: 500.0,
            disk_percent: 42.0,
          },
        ],
        latest_recorded_at: '2026-03-30T10:00:00Z',
        window_start: '2026-03-30T09:00:00Z',
        window_end: '2026-03-30T10:00:00Z',
        bucket_minutes: 5,
      });
    });

    await page.goto('/hosts/host-1');
    await page.getByRole('button', { name: 'Diagnostics', exact: true }).click();

    await expect(page.getByRole('heading', { name: 'CPU', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Memory', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Disk', exact: true })).toBeVisible();
    await expect(page.getByText(/Last sample/i)).toBeVisible();
  });

  test('diagnostics tab shows empty host resource telemetry state', async ({ page }) => {
    await page.goto('/hosts/host-1');
    await page.getByRole('button', { name: 'Diagnostics', exact: true }).click();

    await expect(page.getByText('No telemetry samples in this window')).toBeVisible();
  });
});
