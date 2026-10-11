import { test, expect } from '@playwright/test';
import { launchElectronE2E } from './harness';

// eslint-disable-next-line no-empty-pattern
test('ゲスト起動から API キー接続を設定できる', async ({}, testInfo) => {
  const { harness, stop } = await launchElectronE2E();
  try {
    const { page } = harness;
    await page.waitForURL(/#\/history$/);
    await expect(page.getByRole('link', { name: 'AI接続を設定' })).toBeVisible();

    await page.getByRole('link', { name: 'AI接続を設定' }).click();
    await page.waitForURL(/#\/settings\?section=ai_connection$/);
    // Choosing a suggested model saves it; there is no Save button for it.
    await page.locator('#ai-model').selectOption('gpt-6-luna');
    await expect(page.getByText('モデルを保存しました。')).toBeVisible();
    await page.locator('#ai-api-key').fill('e2e-local-placeholder');
    await page.locator('#ai-api-key + button').click();

    await expect
      .poll(async () => (await harness.readControlSocketStatus()).llm_route)
      .toBe('direct');
    await expect(page.locator('#ai-api-key')).toHaveCount(0);
  } finally {
    await stop({ keepArtifacts: testInfo.status !== testInfo.expectedStatus });
  }
});
