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
    await page.goto('/devices/import');
    await expect(page.getByRole('heading', { name: 'Import devices' })).toBeVisible({ timeout: 10_000 });

    await page.setInputFiles('input[type="file"]', {
      name: 'bundle.json',
      mimeType: 'application/json',
      buffer: Buffer.from(JSON.stringify(BUNDLE_BODY)),
    });

    // After upload the review step shows "New: 1" for valid_new rows
    await expect(page.getByText(/New:\s*1/)).toBeVisible({ timeout: 10_000 });
    await page.getByRole('button', { name: /Commit import/i }).click();
    await expect(page.getByText(/1 created/i)).toBeVisible({ timeout: 10_000 });
  });
});
