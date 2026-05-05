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

  test('system pills and stat cards render', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible({ timeout: 15_000 });

    // System health pills in PageHeader summary — three only
    await expect(page.getByText('Stream', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('DB', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('Grid', { exact: true }).first()).toBeVisible();

    // Explicitly gone
    await expect(page.getByText('Recovery queue', { exact: true })).toHaveCount(0);
    await expect(page.getByText('Queue size', { exact: true })).toHaveCount(0);

    // Stat cards — three only (Hosts, Devices, Sessions)
    await expect(page.getByText('Hosts', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('Devices', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('Sessions', { exact: true }).first()).toBeVisible();
    // "Needs attention" link now lives in the Fleet card and only appears when count > 0,
    // so we no longer assert it has count 0 here.

    await expect(page.getByRole('heading', { name: 'Operations' })).toBeVisible();
    await expect(page.getByTestId('stat-card')).toHaveCount(3);
    await expect(page.getByTestId('system-health-pill')).toHaveCount(3);
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
            operational_state: 'available', hold: null,
            tags: null,
            auto_manage: true,
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

  test('Device recovery shows lifecycle devices and recent incidents', async ({ page }) => {
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
            operational_state: 'offline', hold: null,
            tags: null,
            auto_manage: true,
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
              label: 'Backing Off',
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
            label: 'Backing Off',
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
    await expect(page.getByRole('heading', { name: 'Device recovery' })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText(/1 affected/i)).toBeVisible();
    await expect(page.getByRole('link', { name: 'Backoff Device' }).first()).toBeVisible();
    await expect(page.getByText(/Backing Off/i, { exact: false }).first()).toBeVisible();
    await expect(page.getByRole('link', { name: /^View all$/i }).first()).toBeVisible();

    await expectHeadingStyle(page.getByRole('heading', { name: 'Dashboard' }), '24px');
  });

  test('Device recovery shows empty state', async ({ page }) => {
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
    await expect(page.getByRole('heading', { name: 'Device recovery' })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText('No recovery work right now.')).toBeVisible();
  });

  test('shows skeletons for fleet, recovery, and operations during delayed cold load', async ({ page }) => {
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
    await expect(page.getByRole('status', { name: 'Device recovery loading' })).toBeVisible();
    await expect(page.getByRole('status', { name: 'Operations loading' })).toBeVisible();

    await expect(page.getByRole('heading', { name: 'Fleet', exact: true })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText('No recovery work right now.')).toBeVisible();

    await expect(page.getByRole('status', { name: 'Fleet loading' })).toHaveCount(0);
    await expect(page.getByRole('status', { name: 'Device recovery loading' })).toHaveCount(0);
    await expect(page.getByRole('status', { name: 'Operations loading' })).toHaveCount(0);
  });

  test('page information hierarchy: stat cards appear before fleet card', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible({ timeout: 15_000 });
    const sessionCard = page.getByTestId('stat-card').filter({ hasText: 'Sessions' });
    await expect(sessionCard).toBeVisible();

    const fleetHeading = page.getByRole('heading', { name: 'Fleet', exact: true });
    await expect(fleetHeading).toBeVisible({ timeout: 10_000 });

    const statBox = await sessionCard.boundingBox();
    const fleetBox = await fleetHeading.boundingBox();
    expect(statBox).not.toBeNull();
    expect(fleetBox).not.toBeNull();
    expect(statBox!.y).toBeLessThan(fleetBox!.y);
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

  test('dashboard renders Sessions + Pass rate sparkline elements when data exists', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByRole('heading', { level: 1, name: 'Dashboard' })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('heading', { name: 'Operations' })).toBeVisible();

    // Either the sparkline aria-labels render (analytics returned data), OR the "No runs" placeholder is
    // shown (no pass_rate_pct), OR the per-list idle cell is displayed (no active runs so the
    // "No active runs" idle tile shows in the Active runs column).
    const sessionsSpark = page.getByRole('img', { name: /Sessions last 7 days/ });
    const passRateSpark = page.getByRole('img', { name: /Pass rate last 7 days/ });
    const noRuns = page.getByText('No runs').first();
    const idleCell = page.getByText(/no active runs/i).first();

    await expect(sessionsSpark.or(passRateSpark).or(noRuns).or(idleCell).first()).toBeVisible();
  });

  test('fleet card and recovery card share the same height on desktop', async ({ page }) => {
    // Cards are in a grid with lg:items-stretch so both render at equal height,
    // with the Fleet-health sparkline stretching to fill the extra space.
    await page.route((url) => new URL(url).pathname === '/api/events', async (route) => {
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
    });
    await page.route((url) => new URL(url).pathname === '/api/events/catalog', async (route) => {
      await fulfillJson(route, { events: [] });
    });
    await page.route((url) => new URL(url).pathname === '/api/devices', async (route) => {
      await fulfillJson(route, [
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
          operational_state: 'offline', hold: null,
          tags: null,
          auto_manage: true,
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
            label: 'Backing Off',
            detail: 'Backing off until 2026-03-30T10:10:00Z',
            backoff_until: '2026-03-30T10:10:00Z',
          },
          created_at: '2026-03-30T10:00:00Z',
          updated_at: '2026-03-30T10:00:00Z',
        },
      ]);
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
            label: 'Backing Off',
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
    // Return fewer than 2 points so FleetHealthHistory renders placeholder instead of chart.
    await page.route('**/api/analytics/fleet/capacity-timeline*', async (route) => {
      await fulfillJson(route, { bucket_minutes: 15, series: [] });
    });

    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('heading', { name: 'Fleet', exact: true })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole('heading', { name: /Device recovery/ })).toBeVisible({ timeout: 10_000 });

    // Note: Card component renders with class "shadow-sm" (not "card") — use that as the ancestor selector.
    const fleet = page
      .getByRole('heading', { name: 'Fleet', exact: true })
      .locator('xpath=ancestor::*[contains(@class,"shadow-sm")][1]');
    const recovery = page
      .getByRole('heading', { name: /Device recovery/ })
      .locator('xpath=ancestor::*[contains(@class,"shadow-sm")][1]');
    await expect(fleet).toBeVisible();
    await expect(recovery).toBeVisible();

    const fleetBox = await fleet.boundingBox();
    const recoveryBox = await recovery.boundingBox();
    if (!fleetBox || !recoveryBox) throw new Error('cards not rendered');

    expect(Math.abs(fleetBox.height - recoveryBox.height)).toBeLessThanOrEqual(1);
  });

  test('Fleet health sparkline renders when timeline data exists', async ({ page }) => {
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
            operational_state: 'available', hold: null,
            tags: null,
            auto_manage: true,
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
  });

  test('Operations all-idle strip renders and active-run/busy-device columns are absent', async ({ page }) => {
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
    await expect(page.getByRole('heading', { name: 'Operations' })).toBeVisible({ timeout: 10_000 });

    await expect(page.getByRole('heading', { name: 'Active runs' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Busy devices' })).toBeVisible();
    await expect(page.getByText(/no active runs/i)).toBeVisible();
    await expect(page.getByText(/no busy devices/i)).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Last 7 days' })).toBeVisible();
  });
});
