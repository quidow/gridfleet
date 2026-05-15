import { type Page } from '@playwright/test';
import { test, expect } from './helpers/fixtures';

type SessionRow = {
  id: string;
  session_id: string;
  device_id: string | null;
  device_name: string | null;
  device_platform_id: string | null;
  device_platform_label: string | null;
  test_name: string;
  started_at: string;
  ended_at: string | null;
  status: string;
  requested_pack_id: string | null;
  requested_platform_id: string | null;
  requested_device_type: string | null;
  requested_connection_type: string | null;
  requested_capabilities: Record<string, unknown> | null;
  error_type: string | null;
  error_message: string | null;
  run_id: string | null;
  is_probe?: boolean;
  probe_checked_by?: string | null;
};

async function mockSessionsSurface(page: Page) {
  const sessions: SessionRow[] = [
    {
      id: 'session-setup-failure',
      session_id: 'error-setup-failure-aaaa1111bbbb2222',
      device_id: null,
      device_name: null,
      device_platform_id: null,
      device_platform_label: null,
      test_name: 'test_broken_setup',
      started_at: new Date(Date.UTC(2026, 3, 1, 10, 0, 0)).toISOString(),
      ended_at: new Date(Date.UTC(2026, 3, 1, 10, 1, 0)).toISOString(),
      status: 'error',
      requested_pack_id: 'appium-uiautomator2',
      requested_platform_id: 'android_mobile',
      requested_device_type: 'real_device',
      requested_connection_type: 'network',
      requested_capabilities: {
        platformName: 'Android',
        'appium:automationName': 'UiAutomator2',
      },
      error_type: 'RuntimeError',
      error_message: 'Session could not be created',
      run_id: null,
    },
    ...Array.from({ length: 60 }, (_, index) => ({
      id: `session-${index + 1}`,
      session_id: index === 0 ? 'session-aaaa1111bbbb2222' : `session-${String(index + 1).padStart(4, '0')}`,
      device_id: 'device-1',
      device_name: 'Pixel 8',
      device_platform_id: 'android_mobile',
      device_platform_label: 'Android',
      test_name:
        index === 0 ? 'login.spec.ts' : index === 1 ? 'checkout.spec.ts' : `history-test-${String(index).padStart(2, '0')}`,
      started_at: new Date(Date.UTC(2026, 3, 1, 9 - index, 0, 0)).toISOString(),
      ended_at: new Date(Date.UTC(2026, 3, 1, 9 - index, 12, 0)).toISOString(),
      status: index % 3 === 0 ? 'passed' : index % 3 === 1 ? 'failed' : 'running',
      requested_pack_id: null,
      requested_platform_id: null,
      requested_device_type: null,
      requested_connection_type: null,
      requested_capabilities: null,
      error_type: null,
      error_message: null,
      run_id: null,
    })),
  ];

  await page.addInitScript(() => {
    const fixedNow = new Date('2026-04-01T12:00:00Z').getTime();
    Date.now = () => fixedNow;
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: {
        writeText: async (text: string) => {
          (window as Window & { __copiedSessionId?: string }).__copiedSessionId = text;
        },
      },
    });
  });

  await page.route((url) => new URL(url).pathname === '/api/events', async (route) => {
    await route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
  });
  await page.route((url) => new URL(url).pathname === '/api/events/catalog', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ events: [] }) });
  });
  await page.route('**/api/settings', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
  });
  await page.route((url) => new URL(url).pathname === '/api/devices', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([{ id: 'device-1', name: 'Pixel 8' }]),
    });
  });
  const probeSessions: SessionRow[] = [
    {
      id: 'probe-1',
      session_id: 'probe-0000aaaa1111',
      device_id: 'device-1',
      device_name: 'Pixel 8',
      device_platform_id: 'android_mobile',
      device_platform_label: 'Android',
      test_name: '__gridfleet_probe__',
      started_at: new Date(Date.UTC(2026, 3, 1, 11, 30, 0)).toISOString(),
      ended_at: new Date(Date.UTC(2026, 3, 1, 11, 30, 2)).toISOString(),
      status: 'passed',
      requested_pack_id: null,
      requested_platform_id: null,
      requested_device_type: null,
      requested_connection_type: null,
      requested_capabilities: { 'gridfleet:probeCheckedBy': 'scheduled' },
      error_type: null,
      error_message: null,
      run_id: null,
      is_probe: true,
      probe_checked_by: 'scheduled',
    },
  ];

  await page.route((url) => new URL(url).pathname === '/api/sessions', async (route) => {
    const urlObject = new URL(route.request().url());
    const startedAfter = urlObject.searchParams.get('started_after');
    const startedBefore = urlObject.searchParams.get('started_before');
    const limit = Number(urlObject.searchParams.get('limit') ?? '50');
    const cursor = urlObject.searchParams.get('cursor');
    const direction = urlObject.searchParams.get('direction') ?? 'older';
    const includeProbes = urlObject.searchParams.get('include_probes') === 'true';

    const source = includeProbes ? [...probeSessions, ...sessions] : sessions;
    const filtered = source.filter((session) => {
      const startedAt = new Date(session.started_at).getTime();
      if (startedAfter && startedAt < new Date(startedAfter).getTime()) return false;
      if (startedBefore && startedAt > new Date(startedBefore).getTime()) return false;
      return true;
    });

    const sorted = [...filtered];
    let items = sorted.slice(0, limit);
    if (cursor) {
      const anchorIndex = sorted.findIndex((session) => session.id === cursor);
      if (anchorIndex >= 0 && direction === 'newer') {
        items = sorted.slice(Math.max(0, anchorIndex - limit), anchorIndex);
      } else if (anchorIndex >= 0) {
        items = sorted.slice(anchorIndex + 1, anchorIndex + 1 + limit);
      } else {
        items = [];
      }
    }

    const firstIndex = items.length > 0 ? sorted.findIndex((session) => session.id === items[0].id) : -1;
    const lastIndex = items.length > 0 ? sorted.findIndex((session) => session.id === items[items.length - 1].id) : -1;

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        items,
        limit,
        next_cursor: items.length > 0 && lastIndex < sorted.length - 1 ? items[items.length - 1].id : null,
        prev_cursor: items.length > 0 && firstIndex > 0 ? items[0].id : null,
      }),
    });
  });
}

test.describe('Sessions page', () => {
  test.beforeEach(async ({ page }) => {
    await mockSessionsSurface(page);
  });

  test('loads filters and uses the paginated sessions contract', async ({ page }) => {
    await page.goto('/sessions');

    await expect(page.getByRole('heading', { name: 'Sessions', exact: true })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('option', { name: 'All Devices' })).toBeAttached();
    await expect(page.getByRole('option', { name: 'All Statuses' })).toBeAttached();
    await expect(page.getByRole('option', { name: 'All Platforms' })).toBeAttached();
    await expect(page.getByLabel('Started after')).toBeVisible();
    await expect(page.getByText('Newest results first')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Older' })).toBeEnabled();
  });

  test('shows shortened session IDs with copy action and relative start time', async ({ page }) => {
    await page.goto('/sessions');

    await expect(page.getByText('error-se...bb2222')).toBeVisible();
    await expect(page.getByText('2 hours ago', { exact: true })).toBeVisible();

    await page.getByRole('button', { name: /Copy session ID error-setup-failure-aaaa1111bbbb2222/ }).click();
    await expect
      .poll(() => page.evaluate(() => (window as Window & { __copiedSessionId?: string }).__copiedSessionId ?? null))
      .toBe('error-setup-failure-aaaa1111bbbb2222');
  });

  test('surfaces requested lane and failure detail for device-less setup failures', async ({ page }) => {
    await page.goto('/sessions');

    await expect(page.getByText('Setup failure')).toBeVisible();
    await expect(page.getByText('Android Mobile • Real Device • Network')).toBeVisible();
    await expect(page.getByText('RuntimeError: Session could not be created')).toBeVisible();
    await expect(page.getByText('Synthetic ID')).toBeVisible();
  });

  test('hides probe sessions by default and reveals them when the toggle is enabled', async ({ page }) => {
    await page.goto('/sessions');

    await expect(page.getByRole('heading', { name: 'Sessions', exact: true })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText('__gridfleet_probe__')).toHaveCount(0);
    await expect(page.getByText('probe', { exact: true })).toHaveCount(0);

    await page.getByLabel('Include probe sessions').check();
    await expect(page).toHaveURL(/include_probes=1/);
    await expect(page.getByText('probe', { exact: true })).toBeVisible();
    await expect(page.getByText('scheduled')).toBeVisible();
  });

  test('navigates through cursor history and restores state from the URL', async ({ page }) => {
    await page.goto('/sessions');

    await page.getByRole('button', { name: 'Older' }).click();
    await expect(page).toHaveURL(/cursor=session-49/);
    await expect(page.getByText('Viewing historical results')).toBeVisible();
    await expect(page.getByText('history-test-49')).toBeVisible();

    await page.reload();

    await expect(page).toHaveURL(/cursor=session-49/);
    await expect(page.getByText('Viewing historical results')).toBeVisible();
    await expect(page.getByText('history-test-49')).toBeVisible();

    await page.getByRole('button', { name: 'Back to newest' }).click();
    await expect(page).not.toHaveURL(/cursor=/);
    await expect(page.getByText('Newest results first')).toBeVisible();
  });
});
