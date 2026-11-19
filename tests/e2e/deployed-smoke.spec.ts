import { expect, test } from '@playwright/test';
// Explicit deployed-origin read-only smoke; no provider action or credentials.
test('deployed origin returns the real runtime status page', async ({page}) => {
  await page.goto('/');
  await expect(page.getByRole('heading', {name: 'Runtime status'})).toBeVisible();
});
