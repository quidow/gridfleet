import { type Locator, type Route } from '@playwright/test';
import { test, expect } from './helpers/fixtures';

async function fulfillJson(route: Route, body: unknown, delayMs = 0) {
  if (delayMs > 0) {
    await new Promise((resolve) => setTimeout(resolve, delayMs));
  }
  await route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });
}

async function expectHeadingStyle(
  locator: Locator,
  fontSize: string,
) {
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

test.describe('Dashboard', () => {
  test('loads header with subtitle and actions', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible({ timeout: 15_000 });

    await expect(page.getByText('Fleet overview', { exact: false })).toBeVisible();
    await expectHeadingStyle(page.getByRole('heading', { name: 'Dashboard' }), '24px');
  });

  test('scorecard and main-rail layout render', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible({ timeout: 15_000 });

    // Header pills restored — three
    await expect(page.getByLabel(/^(Stream|DB|Grid) /)).toHaveCount(3);

    // Scorecard cells
    for (const label of ['Hosts', 'Sessions', 'Pass rate · 7d', 'Utilization · 7d']) {
      await expect(page.getByText(label, { exact: true }).first()).toBeVisible();
    }

    // Main + rail
    await expect(page.getByRole('heading', { name: 'Fleet', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: /Needs attention/ })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Activity' })).toBeVisible();
  });

  test('Fleet card renders heading and platform chips when devices exist', async ({ page }) => {
    await page.route((url) => new URL(url).pathname === '/api/events', async (route) => {
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
    });
    await page.route((url) => new URL(url).pathname === '/api/events/catalog', async (route) => {
      await fulfillJson(route, { events: [] });
    });
    await page.route((url) => new URL(url).pathname === '/api/devices', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          {
            id: 'device-1',
            pack_id: 'appium-uiautomator2',
            platform_id: 'android_mobile',
            platform_label: 'Android',
            identity_scheme: 'android_serial',
            identity_scope: 'host',
            identity_value: 'device-1',
            connection_target: 'device-1',
            name: 'Pixel 8',
            os_version: '14',
            host_id: 'host-1',
            operational_state: 'available',
            needs_attention: false,
            device_type: 'real_device',
            connection_type: 'usb',
            ip_address: null,
            battery_level_percent: 90,
            battery_temperature_c: 36,
            charging_state: 'charging',
            hardware_health_status: 'healthy',
            hardware_telemetry_reported_at: '2026-04-16T12:00:00Z',
            hardware_telemetry_state: 'fresh',
            readiness_state: 'verified',
            missing_setup_fields: [],
            verified_at: '2026-04-16T12:00:00Z',
            reservation: null,
            lifecycle_policy_summary: { state: 'idle', label: 'Idle', detail: null, backoff_until: null },
            created_at: '2026-04-16T12:00:00Z',
            updated_at: '2026-04-16T12:00:00Z',
          },
        ]),
      });
    });
    await page.route((url) => new URL(url).pathname === '/api/hosts', async (route) => {
      await fulfillJson(route, []);
    });
    await page.route('**/api/health', async (route) => {
      await fulfillJson(route, { status: 'ok', checks: { database: 'ok' } });
    });
    await page.route('**/api/grid/status', async (route) => {
      await fulfillJson(route, { grid: { ready: true, value: { ready: true, nodes: [] } }, registry: { device_count: 1 }, active_sessions: 0, queue_size: 0 });
    });
    await page.route('**/api/lifecycle/incidents*', async (route) => {
      await fulfillJson(route, { items: [], limit: 20, next_cursor: null, prev_cursor: null });
    });
    await page.route((url) => new URL(url).pathname === '/api/runs', async (route) => {
      await fulfillJson(route, { items: [] });
    });
    await page.route('**/api/analytics/fleet/overview*', async (route) => {
      await fulfillJson(route, { pass_rate_pct: null, avg_utilization_pct: null, devices_needing_attention: 0 });
    });

    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('heading', { name: 'Fleet', exact: true })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole('link', { name: /^Available\b/ })).toBeVisible();
    await expect(page.getByRole('link', { name: /Android/i })).toBeVisible();
  });

  test('needs-attention band shows lifecycle devices and recent incidents', async ({ page }) => {
    await page.route((url) => new URL(url).pathname === '/api/events', async (route) => {
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
    });
    await page.route((url) => new URL(url).pathname === '/api/events/catalog', async (route) => {
      await fulfillJson(route, { events: [] });
    });
    await page.route((url) => new URL(url).pathname === '/api/devices', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          {
            id: 'device-1',
            identity_scheme: 'android_serial',
            identity_scope: 'host',
            identity_value: 'device-1',
            connection_target: 'device-1',
            name: 'Backoff Device',
            pack_id: 'appium-uiautomator2',
            platform_id: 'android_mobile',
            platform_label: 'Android (real device)',
            os_version: '14',
            host_id: 'host-1',
            operational_state: 'offline',
            needs_attention: true,
            device_type: 'real_device',
            connection_type: 'network',
            ip_address: null,
            battery_level_percent: 71,
            battery_temperature_c: 39.4,
            charging_state: 'charging',
            hardware_health_status: 'warning',
            hardware_telemetry_reported_at: '2026-03-30T10:00:00Z',
            hardware_telemetry_state: 'fresh',
            readiness_state: 'verified',
            missing_setup_fields: [],
            verified_at: '2026-03-30T10:00:00Z',
            reservation: null,
            lifecycle_policy_summary: {
              state: 'backoff',
              label: 'Waiting to Retry',
              detail: 'Backing off until 2026-03-30T10:10:00Z',
              backoff_until: '2026-03-30T10:10:00Z',
            },
            created_at: '2026-03-30T10:00:00Z',
            updated_at: '2026-03-30T10:00:00Z',
          },
        ]),
      });
    });
    await page.route((url) => new URL(url).pathname === '/api/hosts', async (route) => {
      await fulfillJson(route, []);
    });
    await page.route('**/api/health', async (route) => {
      await fulfillJson(route, { status: 'ok', checks: { database: 'ok' } });
    });
    await page.route('**/api/grid/status', async (route) => {
      await fulfillJson(route, { grid: { ready: true, value: { ready: true, nodes: [] } }, registry: { device_count: 1 }, active_sessions: 0, queue_size: 0 });
    });
    await page.route('**/api/lifecycle/incidents*', async (route) => {
      await fulfillJson(route, {
        items: [
          {
            id: 'incident-1',
            device_id: 'device-1',
            device_name: 'Backoff Device',
            device_identity_value: 'device-1',
            event_type: 'lifecycle_recovery_backoff',
            label: 'Waiting to Retry',
            summary_state: 'backoff',
            reason: 'Recovery probe failed',
            detail: 'Automatic recovery is backing off before the next retry',
            source: 'session_viability',
            run_id: null,
            run_name: null,
            backoff_until: '2026-03-30T10:10:00Z',
            created_at: '2026-03-30T10:05:00Z',
          },
        ],
        limit: 20,
        next_cursor: null,
        prev_cursor: null,
      });
    });
    await page.route((url) => new URL(url).pathname === '/api/runs', async (route) => {
      await fulfillJson(route, { items: [] });
    });
    await page.route('**/api/analytics/fleet/overview*', async (route) => {
      await fulfillJson(route, { pass_rate_pct: 100, avg_utilization_pct: 25, devices_needing_attention: 1 });
    });

    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('heading', { name: /Needs attention/ })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole('link', { name: 'Backoff Device' }).first()).toBeVisible();
    await expect(page.getByText(/Waiting to Retry/i, { exact: false }).first()).toBeVisible();
    await expect(page.getByRole('link', { name: /^View all$/i }).first()).toBeVisible();

    await expectHeadingStyle(page.getByRole('heading', { name: 'Dashboard' }), '24px');
  });

  test('attention card shows empty state', async ({ page }) => {
    await page.route((url) => new URL(url).pathname === '/api/events', async (route) => {
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
    });
    await page.route((url) => new URL(url).pathname === '/api/events/catalog', async (route) => {
      await fulfillJson(route, { events: [] });
    });
    await page.route((url) => new URL(url).pathname === '/api/devices', async (route) => {
      await fulfillJson(route, []);
    });
    await page.route((url) => new URL(url).pathname === '/api/hosts', async (route) => {
      await fulfillJson(route, []);
    });
    await page.route('**/api/health', async (route) => {
      await fulfillJson(route, { status: 'ok', checks: { database: 'ok' } });
    });
    await page.route('**/api/grid/status', async (route) => {
      await fulfillJson(route, { grid: { ready: true, value: { ready: true, nodes: [] } }, registry: { device_count: 0 }, active_sessions: 0, queue_size: 0 });
    });
    await page.route('**/api/lifecycle/incidents*', async (route) => {
      await fulfillJson(route, { items: [], limit: 20, next_cursor: null, prev_cursor: null });
    });
    await page.route((url) => new URL(url).pathname === '/api/runs', async (route) => {
      await fulfillJson(route, { items: [] });
    });
    await page.route('**/api/analytics/fleet/overview*', async (route) => {
      await fulfillJson(route, { pass_rate_pct: null, avg_utilization_pct: null, devices_needing_attention: 0 });
    });

    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText('Nothing needs attention.')).toBeVisible();
  });

  test('shows skeletons for fleet, attention, and activity during delayed cold load', async ({ page }) => {
    const delayMs = 1_200;

    await page.route((url) => new URL(url).pathname === '/api/events', async (route) => {
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
    });
    await page.route((url) => new URL(url).pathname === '/api/events/catalog', async (route) => {
      await fulfillJson(route, { events: [] });
    });
    await page.route((url) => new URL(url).pathname === '/api/devices', async (route) => {
      await fulfillJson(route, [], delayMs);
    });
    await page.route((url) => new URL(url).pathname === '/api/hosts', async (route) => {
      await fulfillJson(route, [], delayMs);
    });
    await page.route('**/api/health', async (route) => {
      await fulfillJson(route, { status: 'ok', checks: { database: 'ok' } }, delayMs);
    });
    await page.route('**/api/grid/status', async (route) => {
      await fulfillJson(
        route,
        { grid: { ready: true, value: { ready: true, nodes: [] } }, registry: { device_count: 0 }, active_sessions: 0, queue_size: 0 },
        delayMs,
      );
    });
    await page.route('**/api/lifecycle/incidents*', async (route) => {
      await fulfillJson(route, { items: [], limit: 20, next_cursor: null, prev_cursor: null }, delayMs);
    });
    await page.route((url) => new URL(url).pathname === '/api/runs', async (route) => {
      await fulfillJson(route, { items: [] }, delayMs);
    });
    await page.route('**/api/analytics/fleet/overview*', async (route) => {
      await fulfillJson(route, { pass_rate_pct: null, avg_utilization_pct: null, devices_needing_attention: 0 }, delayMs);
    });

    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible({ timeout: 15_000 });

    await expect(page.getByRole('status', { name: 'Fleet loading' })).toBeVisible();
    await expect(page.getByRole('status', { name: 'Attention loading' })).toBeVisible();
    await expect(page.getByRole('status', { name: 'Activity loading' })).toBeVisible();

    await expect(page.getByRole('heading', { name: 'Fleet', exact: true })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText('Nothing needs attention.')).toBeVisible();

    await expect(page.getByRole('status', { name: 'Fleet loading' })).toHaveCount(0);
    await expect(page.getByRole('status', { name: 'Attention loading' })).toHaveCount(0);
    await expect(page.getByRole('status', { name: 'Activity loading' })).toHaveCount(0);
  });

  test('page information hierarchy: scorecard appears before fleet card', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible({ timeout: 15_000 });
    const scoreFact = page.getByText('Pass rate · 7d', { exact: true }).first();
    await expect(scoreFact).toBeVisible();

    const fleetHeading = page.getByRole('heading', { name: 'Fleet', exact: true });
    await expect(fleetHeading).toBeVisible({ timeout: 10_000 });

    const scoreBox = await scoreFact.boundingBox();
    const fleetBox = await fleetHeading.boundingBox();
    expect(scoreBox).not.toBeNull();
    expect(fleetBox).not.toBeNull();
    expect(scoreBox!.y).toBeLessThan(fleetBox!.y);
  });

  test('respects prefers-reduced-motion: reduce on cold load', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible({ timeout: 15_000 });

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

  test('renders the GridFleet app mark in the sidebar', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible({ timeout: 15_000 });
    await expect(page.locator('aside svg[aria-label="GridFleet mark"]')).toBeVisible();
  });

  test('Fleet health chart renders when timeline data exists', async ({ page }) => {
    await page.route((url) => new URL(url).pathname === '/api/events', async (route) => {
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
    });
    await page.route((url) => new URL(url).pathname === '/api/events/catalog', async (route) => {
      await fulfillJson(route, { events: [] });
    });
    await page.route((url) => new URL(url).pathname === '/api/devices', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          {
            id: 'device-1',
            identity_scheme: 'android_serial',
            identity_scope: 'host',
            identity_value: 'device-1',
            connection_target: 'device-1',
            name: 'Pixel 8',
            pack_id: 'appium-uiautomator2',
            platform_id: 'android_mobile',
            platform_label: 'Android (real device)',
            os_version: '14',
            host_id: 'host-1',
            operational_state: 'available',
            needs_attention: false,
            device_type: 'real_device',
            connection_type: 'usb',
            ip_address: null,
            battery_level_percent: 90,
            battery_temperature_c: 36,
            charging_state: 'charging',
            hardware_health_status: 'healthy',
            hardware_telemetry_reported_at: '2026-04-16T12:00:00Z',
            hardware_telemetry_state: 'fresh',
            readiness_state: 'verified',
            missing_setup_fields: [],
            verified_at: '2026-04-16T12:00:00Z',
            reservation: null,
            lifecycle_policy_summary: { state: 'idle', label: 'Idle', detail: null, backoff_until: null },
            created_at: '2026-04-16T12:00:00Z',
            updated_at: '2026-04-16T12:00:00Z',
          },
        ]),
      });
    });
    await page.route((url) => new URL(url).pathname === '/api/hosts', async (route) => {
      await fulfillJson(route, []);
    });
    await page.route('**/api/health', async (route) => {
      await fulfillJson(route, { status: 'ok', checks: { database: 'ok' } });
    });
    await page.route('**/api/grid/status', async (route) => {
      await fulfillJson(route, { grid: { ready: true, value: { ready: true, nodes: [] } }, registry: { device_count: 1 }, active_sessions: 0, queue_size: 0 });
    });
    await page.route('**/api/lifecycle/incidents*', async (route) => {
      await fulfillJson(route, { items: [], limit: 20, next_cursor: null, prev_cursor: null });
    });
    await page.route((url) => new URL(url).pathname === '/api/runs', async (route) => {
      await fulfillJson(route, { items: [] });
    });
    await page.route('**/api/analytics/fleet/overview*', async (route) => {
      await fulfillJson(route, { pass_rate_pct: null, avg_utilization_pct: null, devices_needing_attention: 0 });
    });
    await page.route('**/api/analytics/fleet/capacity-timeline*', async (route) => {
      await fulfillJson(route, {
        bucket_minutes: 15,
        series: [
          {
            timestamp: '2026-04-19T00:00:00Z',
            has_data: true,
            devices_total: 2,
            devices_available: 1,
            devices_offline: 1,
            devices_maintenance: 0,
            hosts_total: 1,
            hosts_online: 1,
            active_sessions: 0,
            queued_requests: 0,
          },
          {
            timestamp: '2026-04-19T00:15:00Z',
            has_data: true,
            devices_total: 2,
            devices_available: 2,
            devices_offline: 0,
            devices_maintenance: 0,
            hosts_total: 1,
            hosts_online: 1,
            active_sessions: 1,
            queued_requests: 0,
          },
        ],
      });
    });

    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('heading', { name: 'Fleet', exact: true })).toBeVisible({ timeout: 10_000 });

    await expect(page.getByText(/^Fleet health$/i).first()).toBeVisible();
    await expect(page.getByRole('img', { name: /Fleet health reachability over last 24 hours/i })).toBeVisible();
    await expect(page.getByRole('link', { name: 'View in Analytics' })).toHaveAttribute(
      'href',
      '/analytics?tab=fleet-capacity',
    );
  });

  test('Activity all-idle renders active-run and busy-device columns', async ({ page }) => {
    await page.route((url) => new URL(url).pathname === '/api/events', async (route) => {
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
    });
    await page.route((url) => new URL(url).pathname === '/api/events/catalog', async (route) => {
      await fulfillJson(route, { events: [] });
    });
    await page.route((url) => new URL(url).pathname === '/api/devices', async (route) => {
      await fulfillJson(route, []);
    });
    await page.route((url) => new URL(url).pathname === '/api/hosts', async (route) => {
      await fulfillJson(route, []);
    });
    await page.route('**/api/health', async (route) => {
      await fulfillJson(route, { status: 'ok', checks: { database: 'ok' } });
    });
    await page.route('**/api/grid/status', async (route) => {
      await fulfillJson(route, { grid: { ready: true, value: { ready: true, nodes: [] } }, registry: { device_count: 0 }, active_sessions: 0, queue_size: 0 });
    });
    await page.route('**/api/lifecycle/incidents*', async (route) => {
      await fulfillJson(route, { items: [], limit: 20, next_cursor: null, prev_cursor: null });
    });
    await page.route((url) => new URL(url).pathname === '/api/runs', async (route) => {
      await fulfillJson(route, { items: [] });
    });
    await page.route('**/api/analytics/fleet/overview*', async (route) => {
      await fulfillJson(route, { pass_rate_pct: null, avg_utilization_pct: null, devices_needing_attention: 0 });
    });

    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('heading', { name: 'Activity' })).toBeVisible({ timeout: 10_000 });

    await expect(page.getByRole('heading', { name: 'Active runs · none' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Busy outside runs · none' })).toBeVisible();
  });
});
