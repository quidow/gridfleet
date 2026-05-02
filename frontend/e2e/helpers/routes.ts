import type { Page, Route } from '@playwright/test';

type AuthSession = {
  enabled: boolean;
  authenticated: boolean;
  username: string | null;
  csrf_token: string | null;
  expires_at: string | null;
};

const AUTH_DISABLED_SESSION: AuthSession = {
  enabled: false,
  authenticated: false,
  username: null,
  csrf_token: null,
  expires_at: null,
};

const DEFAULT_DRIVER_PACK_CATALOG = {
  packs: [
    {
      id: 'appium-uiautomator2',
      display_name: 'Appium UiAutomator2',
      state: 'enabled',
      current_release: '2026.04.0',
      platforms: [
        {
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
          health_checks: [
            { id: 'adb_connected', label: 'ADB Connected' },
            { id: 'adb_responsive', label: 'ADB Responsive' },
            { id: 'boot_completed', label: 'Boot Completed' },
            { id: 'ping', label: 'IP Reachable' },
          ],
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
        },
      ],
    },
    {
      id: 'appium-xcuitest',
      display_name: 'Appium XCUITest',
      state: 'enabled',
      current_release: '2026.04.0',
      platforms: [
        {
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
          health_checks: [
            { id: 'devicectl_visible', label: 'devicectl Visible' },
            { id: 'devicectl_paired', label: 'Device Paired' },
            { id: 'devicectl_tunnel', label: 'CoreDevice Tunnel' },
            { id: 'ios_booted', label: 'OS Booted' },
            { id: 'developer_mode', label: 'Developer Mode' },
            { id: 'ddi_services', label: 'Developer Services' },
            { id: 'simulator_booted', label: 'Simulator Booted' },
            { id: 'simulator_responsive', label: 'Simulator Responsive' },
            { id: 'ping', label: 'IP Reachable' },
          ],
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
        },
        {
          id: 'tvos',
          display_name: 'tvOS',
          automation_name: 'XCUITest',
          appium_platform_name: 'tvOS',
          device_types: ['real_device', 'simulator'],
          connection_types: ['usb', 'network', 'virtual'],
          grid_slots: ['native'],
          identity_scheme: 'apple_udid',
          identity_scope: 'global',
          discovery_kind: 'apple',
          lifecycle_actions: [{ id: 'state' }, { id: 'reconnect' }],
          health_checks: [
            { id: 'devicectl_visible', label: 'devicectl Visible' },
            { id: 'devicectl_paired', label: 'Device Paired' },
            { id: 'ios_booted', label: 'OS Booted' },
            { id: 'developer_mode', label: 'Developer Mode' },
            { id: 'simulator_booted', label: 'Simulator Booted' },
            { id: 'simulator_responsive', label: 'Simulator Responsive' },
            { id: 'ping', label: 'IP Reachable' },
          ],
          device_fields_schema: [],
          capabilities: {},
          display_metadata: { icon_kind: 'tv' },
          default_capabilities: {},
          connection_behavior: { default_device_type: 'real_device', default_connection_type: 'usb', requires_connection_target: true },
          device_type_overrides: {
              real_device: {
                default_capabilities: {
                  'appium:platformVersion': '{device.os_version}',
                  'appium:usePreinstalledWDA': true,
                },
              device_fields_schema: [
                {
                  id: 'wda_base_url',
                  label: 'WDA base URL',
                  type: 'network_endpoint',
                  required_for_session: true,
                  capability_name: 'appium:wdaBaseUrl',
                },
                {
                  id: 'use_preinstalled_wda',
                  label: 'Use pre-installed WDA',
                  type: 'bool',
                  default: true,
                  capability_name: 'appium:usePreinstalledWDA',
                },
                {
                  id: 'updated_wda_bundle_id',
                  label: 'Updated WDA bundle ID',
                  type: 'string',
                  required_for_session: true,
                  capability_name: 'appium:updatedWDABundleId',
                },
              ],
              connection_behavior: { default_device_type: 'real_device', default_connection_type: 'usb', requires_connection_target: true },
            },
            simulator: {
              identity: { scheme: 'simulator_udid', scope: 'host' },
              lifecycle_actions: [{ id: 'state' }, { id: 'boot' }, { id: 'shutdown' }],
              connection_behavior: { default_device_type: 'simulator', default_connection_type: 'virtual', requires_connection_target: true },
            },
          },
        },
      ],
    },
    {
      id: 'appium-roku-dlenroc',
      display_name: 'Roku (dlenroc)',
      state: 'enabled',
      current_release: '2026.04.0',
      platforms: [
        {
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
          health_checks: [
            { id: 'ping', label: 'IP Reachable' },
            { id: 'ecp', label: 'ECP Reachable' },
            { id: 'developer_mode', label: 'Developer Mode' },
          ],
          device_fields_schema: [
            {
              id: 'roku_password',
              label: 'Developer password',
              type: 'string',
              required_for_session: true,
              sensitive: true,
              capability_name: 'appium:password',
            },
          ],
          capabilities: {},
          display_metadata: { icon_kind: 'set_top' },
          default_capabilities: { 'appium:ip': '{device.ip_address}' },
          connection_behavior: {
            default_device_type: 'real_device',
            default_connection_type: 'network',
            requires_ip_address: true,
            requires_connection_target: false,
          },
        },
      ],
    },
  ],
};

export async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });
}

function paginatedEmptyResponse(requestUrl: URL) {
  return {
    items: [],
    total: 0,
    limit: Number(requestUrl.searchParams.get('limit') ?? 50),
    offset: Number(requestUrl.searchParams.get('offset') ?? 0),
  };
}

export async function mockDefaultApiFallbacks(page: Page) {
  await page.route((url) => new URL(url).pathname.startsWith('/api/'), async (route) => {
    const request = route.request();
    const requestUrl = new URL(request.url());
    const path = requestUrl.pathname;
    const method = request.method();

    if (method !== 'GET') {
      await fulfillJson(
        route,
        {
          error: {
            code: 'UNHANDLED_E2E_ROUTE',
            message: `Unhandled mocked e2e route: ${method} ${path}`,
          },
        },
        404,
      );
      return;
    }

    if (path === '/api/auth/session') {
      await fulfillJson(route, AUTH_DISABLED_SESSION);
      return;
    }

    if (path === '/api/events') {
      await fulfillEventStream(route);
      return;
    }

    if (path === '/api/events/catalog') {
      await fulfillJson(route, { events: [] });
      return;
    }

    if (path === '/api/settings' || path === '/api/hosts' || path === '/api/devices' || path === '/api/webhooks') {
      await fulfillJson(route, []);
      return;
    }

    if (path === '/api/sessions' || path === '/api/runs') {
      await fulfillJson(route, paginatedEmptyResponse(requestUrl));
      return;
    }

    if (/^\/api\/hosts\/[^/]+\/intake-candidates$/.test(path)) {
      await fulfillJson(route, []);
      return;
    }

    if (/^\/api\/hosts\/[^/]+\/driver-packs$/.test(path)) {
      await fulfillJson(route, []);
      return;
    }

    if (path === '/api/health') {
      await fulfillJson(route, { status: 'ok', checks: { database: 'ok' } });
      return;
    }

    if (path === '/api/grid/status') {
      await fulfillJson(route, {
        grid: { ready: true, value: { ready: true, nodes: [] } },
        registry: { device_count: 0 },
        active_sessions: 0,
        queue_size: 0,
      });
      return;
    }

    if (path === '/api/lifecycle/incidents') {
      await fulfillJson(route, { items: [], limit: 20, next_cursor: null, prev_cursor: null });
      return;
    }

    if (path === '/api/analytics/sessions/summary') {
      await fulfillJson(route, []);
      return;
    }

    if (path === '/api/analytics/devices/utilization') {
      await fulfillJson(route, []);
      return;
    }

    if (path === '/api/analytics/devices/reliability') {
      await fulfillJson(route, []);
      return;
    }

    if (path === '/api/analytics/fleet/overview') {
      await fulfillJson(route, {
        pass_rate_pct: null,
        avg_utilization_pct: null,
        devices_needing_attention: 0,
      });
      return;
    }

    if (path === '/api/analytics/fleet/capacity-timeline') {
      await fulfillJson(route, { buckets: [] });
      return;
    }

    if (path === '/api/driver-packs/catalog') {
      await fulfillJson(route, DEFAULT_DRIVER_PACK_CATALOG);
      return;
    }

    const driverPackDetailMatch = path.match(/^\/api\/driver-packs\/([^/]+)$/);
    if (driverPackDetailMatch) {
      const packId = decodeURIComponent(driverPackDetailMatch[1]);
      const pack = DEFAULT_DRIVER_PACK_CATALOG.packs.find((entry) => entry.id === packId);
      if (pack) {
        await fulfillJson(route, pack);
      } else {
        await fulfillJson(route, { detail: `Pack ${packId} not found` }, 404);
      }
      return;
    }

    await fulfillJson(
      route,
      {
        error: {
          code: 'UNHANDLED_E2E_ROUTE',
          message: `Unhandled mocked e2e route: ${method} ${path}`,
        },
      },
      404,
    );
  });
}

async function fulfillEventStream(route: Route, body = '') {
  await route.fulfill({
    status: 200,
    contentType: 'text/event-stream',
    body,
  });
}

export async function mockEventsApi(page: Page, events: unknown[] = []) {
  await page.route((url) => new URL(url).pathname === '/api/events', async (route) => {
    await fulfillEventStream(route);
  });

  await page.route((url) => new URL(url).pathname === '/api/events/catalog', async (route) => {
    await fulfillJson(route, { events });
  });
}

export async function mockEmptySettingsApi(page: Page) {
  await page.route('**/api/settings', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    await fulfillJson(route, []);
  });
}

async function mockEmptyHostsApi(page: Page) {
  await page.route('**/api/hosts', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    await fulfillJson(route, []);
  });
}

async function mockEmptyWebhooksApi(page: Page) {
  await page.route('**/api/webhooks', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    await fulfillJson(route, []);
  });
}

export async function mockSettingsChromeApis(page: Page) {
  await mockEventsApi(page);
  await mockEmptySettingsApi(page);
  await mockEmptyHostsApi(page);
  await mockEmptyWebhooksApi(page);
}

export async function mockAppShellApis(page: Page) {
  await mockEventsApi(page);
  await mockEmptySettingsApi(page);
  await mockEmptyHostsApi(page);

  await page.route((url) => new URL(url).pathname === '/api/devices', async (route) => {
    await fulfillJson(route, []);
  });

  await page.route((url) => new URL(url).pathname === '/api/sessions', async (route) => {
    await fulfillJson(route, { items: [], total: 0, limit: 50, offset: 0 });
  });

  await page.route((url) => new URL(url).pathname === '/api/runs', async (route) => {
    await fulfillJson(route, { items: [], total: 0, limit: 50, offset: 0 });
  });

  await page.route('**/api/analytics/sessions/summary**', async (route) => {
    await fulfillJson(route, []);
  });

  await page.route('**/api/analytics/devices/utilization**', async (route) => {
    await fulfillJson(route, []);
  });

  await page.route('**/api/analytics/devices/reliability**', async (route) => {
    await fulfillJson(route, []);
  });

  await page.route('**/api/analytics/fleet/overview**', async (route) => {
    await fulfillJson(route, {
      pass_rate_pct: null,
      avg_utilization_pct: null,
      devices_needing_attention: 0,
    });
  });

  await page.route('**/api/analytics/fleet/capacity-timeline**', async (route) => {
    await fulfillJson(route, { buckets: [] });
  });

  await page.route('**/api/health', async (route) => {
    await fulfillJson(route, { status: 'ok', checks: { database: 'ok' } });
  });

  await page.route('**/api/grid/status', async (route) => {
    await fulfillJson(route, {
      grid: { ready: true, value: { ready: true, nodes: [] } },
      registry: { device_count: 0 },
      active_sessions: 0,
      queue_size: 0,
    });
  });

  await page.route('**/api/lifecycle/incidents*', async (route) => {
    await fulfillJson(route, { items: [], limit: 20, next_cursor: null, prev_cursor: null });
  });
}
