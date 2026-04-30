import { test, expect } from './helpers/fixtures';
import { mockAppShellApis } from './helpers/routes';

test.describe('Analytics page', () => {
  test.beforeEach(async ({ page }) => {
    await mockAppShellApis(page);
  });

  test('loads and shows heading', async ({ page }) => {
    await page.goto('/analytics');
    await expect(page.getByRole('heading', { name: 'Analytics' })).toBeVisible({ timeout: 15_000 });
  });

  test('tabs are present and switchable', async ({ page }) => {
    await page.goto('/analytics');
    await expect(page.getByRole('heading', { name: 'Analytics' })).toBeVisible({ timeout: 15_000 });

    // All three tabs should be visible
    await expect(page.getByRole('button', { name: 'Session Trends' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Device Utilization' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Reliability' })).toBeVisible();

    // Click Device Utilization tab
    await page.getByRole('button', { name: 'Device Utilization' }).click();
    expect(page.url()).toContain('tab=utilization');

    // Click Reliability tab
    await page.getByRole('button', { name: 'Reliability' }).click();
    expect(page.url()).toContain('tab=reliability');
  });

  test('date range presets are present', async ({ page }) => {
    await page.goto('/analytics');
    await expect(page.getByRole('heading', { name: 'Analytics' })).toBeVisible({ timeout: 15_000 });

    await expect(page.getByRole('button', { name: 'Last 24h' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Last 7 days' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Last 30 days' })).toBeVisible();
  });

  test('defaults to last 7 days and can switch back to custom', async ({ page }) => {
    await page.goto('/analytics');
    await expect(page.getByRole('heading', { name: 'Analytics' })).toBeVisible({ timeout: 15_000 });

    await expect(page.getByRole('button', { name: 'Last 7 days' })).toHaveClass(/bg-accent/);
    await expect(page.getByLabel('Analytics date from')).toHaveCount(0);
    await expect(page.getByLabel('Analytics date to')).toHaveCount(0);

    await page.getByRole('button', { name: 'Last 24h' }).click();
    await expect(page).toHaveURL(/preset=24h/);
    await expect(page.getByLabel('Analytics date from')).toHaveCount(0);
    await expect(page.getByLabel('Analytics date to')).toHaveCount(0);

    await page.getByRole('button', { name: 'Custom' }).click();
    await expect(page).toHaveURL(/preset=custom/);
    await expect(page.getByRole('button', { name: 'Custom' })).toHaveClass(/bg-accent/);
    await expect(page.getByLabel('Analytics date from')).toBeVisible();
    await expect(page.getByLabel('Analytics date to')).toBeVisible();
  });

  test('session trends tab shows content', async ({ page }) => {
    await page.goto('/analytics');
    await expect(page.getByRole('heading', { name: 'Analytics' })).toBeVisible({ timeout: 15_000 });

    // Should show session trends content (either charts or empty state)
    await expect(page.getByText('Sessions per Day')).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText('Sessions by Platform')).toBeVisible();
  });

  test('reliability tab shows content', async ({ page }) => {
    await page.goto('/analytics?tab=reliability');
    await expect(page.getByRole('heading', { name: 'Analytics' })).toBeVisible({ timeout: 15_000 });

    // Wait for reliability content to load
    await expect(page.getByText('Device Reliability')).toBeVisible({ timeout: 10_000 });
  });

  test('custom range uses native date inputs and empty analytics states show guidance', async ({ page }) => {
    await page.route('**/api/analytics/sessions/summary**', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    });
    await page.route('**/api/analytics/devices/utilization**', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    });
    await page.route('**/api/analytics/devices/reliability**', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
    });

    await page.goto('/analytics');
    await page.getByRole('button', { name: 'Custom' }).click();

    // Native <input type="date"> accepts and reports dates in YYYY-MM-DD format.
    await page.getByLabel('Analytics date from').fill('2026-04-02');
    await page.getByLabel('Analytics date to').fill('2026-04-03');

    await expect(page.getByLabel('Analytics date from')).toHaveValue('2026-04-02');
    await expect(page.getByLabel('Analytics date to')).toHaveValue('2026-04-03');
    await expect(page.getByText('No sessions in this period')).toBeVisible();

    await page.getByRole('button', { name: 'Reliability' }).click();
    await expect(page.getByText('No incidents recorded in this period')).toBeVisible();
    await expect(page.getByText(/widening the window/i)).toBeVisible();
  });
});

test.describe('Dashboard analytics widgets', () => {
  test.beforeEach(async ({ page }) => {
    await mockAppShellApis(page);
  });

  test('shows analytics summary cards', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible({ timeout: 15_000 });

    // The fleet overview widgets should appear
    await expect(page.getByText('Last 7 days')).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText('Pass rate')).toBeVisible();
    await expect(page.getByText('Fleet utilization')).toBeVisible();
    await expect(page.getByText('Reliability watchlist')).toBeVisible();
  });
});
