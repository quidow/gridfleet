import { test, expect } from './helpers/fixtures';
import { mockAppShellApis } from './helpers/routes';

test.describe('Device inventory export modal', () => {
  test.beforeEach(async ({ page }) => {
    await mockAppShellApis(page);
  });

  test('modal downloads csv with format query param', async ({ page }) => {
    let capturedUrl = '';
    await page.route('**/api/devices/inventory**', async (route) => {
      capturedUrl = route.request().url();
      await route.fulfill({
        status: 200,
        contentType: 'text/csv',
        body: 'name,host.hostname\nPixel,lab-04\n',
      });
    });

    await page.goto('/devices');
    await expect(page.getByRole('heading', { name: 'Devices', exact: true })).toBeVisible({ timeout: 15_000 });

    await page.getByRole('button', { name: /export inventory/i }).click();
    await expect(page.getByRole('dialog', { name: /export inventory/i })).toBeVisible();

    // Select CSV format (it is the default but let's make it explicit)
    await page.getByLabel('CSV').check();

    const downloadPromise = page.waitForEvent('download');
    await page.getByRole('button', { name: /^download$/i }).click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toMatch(/^gridfleet-inventory-.*\.csv$/);
    expect(capturedUrl).toContain('format=csv');
  });
});
