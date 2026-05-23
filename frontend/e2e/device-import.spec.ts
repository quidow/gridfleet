import { test, expect } from './helpers/fixtures';
import { mockAppShellApis } from './helpers/routes';

const BUNDLE_BODY = {
  schema_version: 1,
  exported_at: '2026-05-23T00:00:00Z',
  source_instance: 'alpha',
  devices: [
    {
      pack_id: 'appium-uiautomator2',
      platform_id: 'android_mobile',
      identity_scheme: 'android_serial',
      identity_scope: 'host',
      identity_value: 'R58',
      name: 'Pixel 7',
      device_type: 'real_device',
      connection_type: 'usb',
      auto_manage: true,
      tags: {},
      device_config: {},
      test_data: {},
      original_host: { hostname: 'lab-04' },
    },
  ],
};

test.describe('Device import wizard', () => {
  test.beforeEach(async ({ page }) => {
    await mockAppShellApis(page);
    await page.route('**/api/devices/import/validate', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          schema_version: 1,
          exported_at: '2026-05-23T00:00:00Z',
          bundle_hash: 'sha256:abcd',
          available_hosts: [{ id: 'host-1', hostname: 'lab-04' }],
          rows: [
            {
              index: 0,
              device: BUNDLE_BODY.devices[0],
              status: 'valid_new',
              host_suggestion: { id: 'host-1', hostname: 'lab-04' },
              issues: [],
            },
          ],
        }),
      });
    });
    await page.route('**/api/devices/import', async (route) => {
      if (route.request().method() !== 'POST') {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          created: [{ index: 0, device_id: 'device-1' }],
          skipped: [],
          failed: [],
        }),
      });
    });
  });

  test('upload → review → commit', async ({ page }) => {
    await page.goto('/settings?tab=backup');
    await expect(page.getByRole('heading', { name: /backup & restore/i })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole('heading', { name: /step 1.*upload bundle/i })).toBeVisible();

    await page.setInputFiles('input#import-bundle', {
      name: 'bundle.json',
      mimeType: 'application/json',
      buffer: Buffer.from(JSON.stringify(BUNDLE_BODY)),
    });

    await expect(page.getByText(/1 new/i)).toBeVisible({ timeout: 10_000 });
    // Pick the suggested host for row 0
    await page.getByLabel('host-0').selectOption('host-1');
    await page.getByRole('button', { name: /commit import/i }).click();
    await expect(page.getByText(/1 created/i)).toBeVisible({ timeout: 10_000 });
  });
});
