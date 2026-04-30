import { expect, test } from './helpers/fixtures';
import { fulfillJson, mockSettingsChromeApis } from './helpers/routes';

test('Drivers page lists appium-uiautomator2 driver from catalog', async ({ page }) => {
  await page.route('**/api/driver-packs/catalog', async (route) => {
    await fulfillJson(route, {
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
    });
  });

  await mockSettingsChromeApis(page);

  await page.goto('/drivers');
  await expect(page.getByRole('heading', { name: 'Driver Packs', exact: true })).toBeVisible();
  await expect(page.getByText('Appium UiAutomator2')).toBeVisible();
  await expect(page.getByText('2026.04.0')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Upload Driver' })).toBeVisible();
});
