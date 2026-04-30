import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const ROUTES = ['/', '/devices', '/sessions', '/runs', '/analytics', '/notifications', '/settings'];

for (const route of ROUTES) {
  test(`has no critical automated a11y violations on ${route}`, async ({ page }) => {
    await page.goto(route);

    const results = await new AxeBuilder({ page })
      .disableRules(['color-contrast'])
      .analyze();

    expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
  });
}
