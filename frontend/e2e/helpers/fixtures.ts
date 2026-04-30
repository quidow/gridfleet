import { test as base, expect } from '@playwright/test';
import { mockDefaultApiFallbacks } from './routes';

export const test = base.extend({
  page: async ({ page }, runFixture) => {
    await mockDefaultApiFallbacks(page);
    await runFixture(page);
  },
});

export { expect };
