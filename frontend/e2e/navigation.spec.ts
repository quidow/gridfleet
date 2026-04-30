import { test, expect } from './helpers/fixtures';
import { mockAppShellApis } from './helpers/routes';

test.describe('Navigation', () => {
  test.beforeEach(async ({ page }) => {
    await mockAppShellApis(page);
  });

  test('sidebar links navigate to correct pages', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible({ timeout: 15_000 });

    // Navigate to Devices
    await page.getByRole('link', { name: 'Devices', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 10_000 });
    expect(page.url()).toContain('/devices');

    // Navigate to Hosts
    await page.getByRole('link', { name: 'Hosts' }).click();
    await expect(page.getByRole('heading', { name: 'Hosts', exact: true })).toBeVisible({ timeout: 10_000 });
    expect(page.url()).toContain('/hosts');

    // Navigate to Sessions
    await page.getByRole('link', { name: 'Sessions', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Sessions', exact: true })).toBeVisible({ timeout: 10_000 });
    expect(page.url()).toContain('/sessions');

    // Navigate to Analytics
    await page.getByRole('link', { name: 'Analytics' }).click();
    await expect(page.getByRole('heading', { name: 'Analytics' })).toBeVisible({ timeout: 10_000 });
    expect(page.url()).toContain('/analytics');

    // Navigate to Drivers
    await page.getByRole('link', { name: 'Drivers' }).click();
    await expect(page.getByRole('heading', { name: 'Driver Packs', exact: true })).toBeVisible({ timeout: 10_000 });
    expect(page.url()).toContain('/drivers');

    // Navigate to Settings
    await page.getByRole('link', { name: 'Settings' }).click();
    await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible({ timeout: 10_000 });
    expect(page.url()).toContain('/settings');
    await expect(page.getByRole('link', { name: 'Webhooks' })).toHaveCount(0);

    // Navigate back to Dashboard
    await page.getByRole('link', { name: 'Dashboard' }).click();
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible({ timeout: 10_000 });
  });

  test('404 page for unknown route', async ({ page }) => {
    await page.goto('/some-nonexistent-page');
    await expect(page.getByText(/not found/i)).toBeVisible({ timeout: 10_000 });
  });
});
