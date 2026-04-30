import { expect, test } from './helpers/fixtures';
import { fulfillJson, mockSettingsChromeApis } from './helpers/routes';

const UIAUTOMATOR2_PACK = {
  id: 'appium-uiautomator2',
  display_name: 'Appium UiAutomator2',
  state: 'enabled',
  active_runs: 0,
  live_sessions: 0,
  current_release: '2026.04.0',
  platforms: [],
  insecure_features: [],
};

const ROKU_PACK = {
  id: 'appium-roku-dlenroc',
  display_name: 'Roku (dlenroc)',
  state: 'disabled',
  active_runs: 0,
  live_sessions: 0,
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
      device_fields_schema: [],
      capabilities: {},
      display_metadata: {},
      default_capabilities: {},
    },
  ],
  insecure_features: [],
};

test.describe('Roku pack enable/disable', () => {
  test('enable toggle works for Roku pack', async ({ page }) => {
    let rokuState = 'disabled';

    await mockSettingsChromeApis(page);

    await page.route('**/api/driver-packs/catalog', async (route) => {
      await fulfillJson(route, {
        packs: [UIAUTOMATOR2_PACK, { ...ROKU_PACK, state: rokuState }],
      });
    });

    await page.route('**/api/driver-packs/appium-roku-dlenroc', async (route) => {
      if (route.request().method() === 'PATCH') {
        rokuState = rokuState === 'disabled' ? 'enabled' : 'disabled';
        await fulfillJson(route, { ...ROKU_PACK, state: rokuState });
      } else if (route.request().method() === 'GET') {
        await fulfillJson(route, { ...ROKU_PACK, state: rokuState });
      } else {
        await route.fallback();
      }
    });

    await page.goto('/drivers/appium-roku-dlenroc');

    // Roku detail page shows "disabled" badge and "Enable" button.
    await expect(page.getByRole('heading', { name: 'Roku (dlenroc)' })).toBeVisible();
    await expect(page.getByText('disabled')).toBeVisible();
    const enableButton = page.getByRole('button', { name: /enable/i });
    await expect(enableButton).toBeVisible();

    // Click to enable.
    await enableButton.click();

    // After toggle, Roku detail should show "enabled" badge and "Disable" button.
    await expect(page.getByText('enabled')).toBeVisible();
    await expect(page.getByRole('button', { name: /disable/i })).toBeVisible();
  });
});
