import { test, expect } from '@playwright/test';

test.describe('Live stack smoke', () => {
  test('dashboard, devices, and runs load against the self-started stack', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible({ timeout: 15_000 });

    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('button', { name: 'Add Device' })).toBeVisible();

    await page.goto('/runs');
    await expect(page.getByRole('heading', { name: 'Test Runs', exact: true })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByLabel('Created from')).toBeVisible();
  });
});
