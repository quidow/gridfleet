import { expect, test } from './helpers/fixtures';
import { fulfillJson } from './helpers/routes';

const CATALOG_WITH_ONE_PACK = {
  packs: [
    {
      id: 'appium-uiautomator2',
      display_name: 'Appium UiAutomator2',
      state: 'enabled',
      active_runs: 0,
      live_sessions: 0,
      current_release: '2026.04.0',
    },
  ],
};

test('add-driver dialog is upload-only and hides template picker UI', async ({ page }) => {
  await page.route('**/api/driver-packs/catalog', async (route) => {
    await fulfillJson(route, CATALOG_WITH_ONE_PACK);
  });

  await page.goto('/drivers');

  await page.getByRole('button', { name: 'Upload Driver' }).click();

  await expect(page.getByRole('dialog', { name: 'Upload Driver Pack' })).toBeVisible();
  await expect(page.getByLabel('Driver tarball')).toBeVisible();
  await expect(page.getByText('Choose a driver or template')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Use this template' })).toHaveCount(0);
});
