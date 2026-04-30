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

const CATALOG_WITH_UPLOADED_PACK = {
  packs: [
    ...CATALOG_WITH_ONE_PACK.packs,
    {
      id: 'vendor-foo',
      display_name: 'Vendor Foo',
      state: 'enabled',
      active_runs: 0,
      live_sessions: 0,
      current_release: null,
    },
  ],
};

test('admin uploads a tarball driver pack and sees it in the catalog', async ({ page }) => {
  let uploadDone = false;

  // Catalog returns one pack initially; after upload it includes the new pack
  await page.route('**/api/driver-packs/catalog', async (route) => {
    await fulfillJson(route, uploadDone ? CATALOG_WITH_UPLOADED_PACK : CATALOG_WITH_ONE_PACK);
  });

  // Accept multipart upload, return 201 with the new pack
  await page.route('**/api/driver-packs/uploads', async (route) => {
    if (route.request().method() !== 'POST') {
      await route.fallback();
      return;
    }
    uploadDone = true;
    await fulfillJson(
      route,
      {
        id: 'vendor-foo',
        display_name: 'Vendor Foo',
        state: 'enabled',
      },
      201,
    );
  });

  await page.goto('/drivers');

  // Step 1: the catalog table is visible and "Upload Driver" button is present.
  await expect(page.getByRole('button', { name: 'Upload Driver' })).toBeVisible();

  // Step 2: open the AddDriverDialog.
  await page.getByRole('button', { name: 'Upload Driver' }).click();
  const dialog = page.getByRole('dialog', { name: 'Upload Driver Pack' });
  await expect(dialog).toBeVisible();

  // Step 3: the upload form is rendered directly.
  await expect(page.getByRole('button', { name: 'Upload tarball' })).toHaveCount(0);

  // Step 4: select the fake tarball file
  const fileInput = page.locator('#driver-tarball');
  await fileInput.setInputFiles({
    name: 'vendor-foo-0.1.0.tar.gz',
    mimeType: 'application/gzip',
    buffer: Buffer.from('fake tarball bytes'),
  });

  // Step 5: check the confirmation checkbox
  await page.getByRole('checkbox', {
    name: 'I confirm this driver may execute Python code on host machines.',
  }).check();

  // Step 6: submit
  await dialog.getByRole('button', { name: 'Upload driver', exact: true }).click();

  // Step 7: dialog closes (upload form is gone) and catalog table now contains "Vendor Foo"
  await expect(dialog.getByRole('button', { name: 'Upload driver', exact: true })).not.toBeVisible();
  await expect(page.getByText('Vendor Foo')).toBeVisible();
});
