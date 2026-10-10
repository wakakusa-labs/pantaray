import { test, expect, type Page } from '@playwright/test';
import { launchElectronE2E } from './harness';

// The composer frame's position on screen and the overlay window's height.
function measure(overlay: Page) {
  return overlay.evaluate(() => ({
    frameTop:
      window.screenY + document.querySelector('form.overlay-composer')!.getBoundingClientRect().top,
    height: window.outerHeight,
  }));
}

// eslint-disable-next-line no-empty-pattern
test('opening and closing the project list never moves the composer on screen', async ({}, testInfo) => {
  const { harness, stop } = await launchElectronE2E();
  try {
    const { app, page } = harness;
    await page.waitForURL(/#\/history$/);
    await expect
      .poll(() =>
        page.evaluate(() =>
          window
            .electron!.workspaceSettings!.createProject({
              displayName: 'Aurora Web',
              organizationIds: [],
            })
            .then(
              () => 'created',
              (error: Error) => error.message
            )
        )
      )
      .toBe('created');
    await page.evaluate(() => window.electron!.history!.openNewConversation());
    await expect
      .poll(() => app.windows().some((w) => w.url().includes('mode=standalone')))
      .toBe(true);
    const overlay = app.windows().find((w) => w.url().includes('mode=standalone'))!;
    const input = overlay.getByRole('textbox', { name: 'メッセージ', exact: true });
    const listbox = overlay.getByRole('listbox', { name: 'プロジェクト' });
    await expect(input).toBeVisible();
    await overlay.waitForTimeout(500);

    const check = async (placement: 'above' | 'below') => {
      const before = await measure(overlay);
      await input.click();
      await input.press('End');
      await overlay.keyboard.type(' @au');
      await expect(listbox.getByRole('option', { name: 'Aurora Web' })).toBeVisible();
      const listBox = (await listbox.boundingBox())!;
      const frameBox = (await overlay.locator('form.overlay-composer').boundingBox())!;
      expect(placement === 'above' ? listBox.y < frameBox.y : listBox.y > frameBox.y).toBe(true);
      if (placement === 'below') {
        await expect
          .poll(async () => (await measure(overlay)).height)
          .toBeGreaterThan(before.height);
      }
      await overlay.waitForTimeout(300);
      const open = await measure(overlay);
      await overlay.keyboard.press('Enter');
      await expect(listbox).toHaveCount(0);
      await expect.poll(async () => (await measure(overlay)).height).toBe(before.height);
      const closed = await measure(overlay);
      console.log(`[mention-window] ${placement}`, JSON.stringify({ before, open, closed }));
      expect(open.frameTop).toBe(before.frameTop);
      expect(closed.frameTop).toBe(before.frameTop);
    };

    // A new conversation is a small window: the list opens below and the window grows downward.
    await input.fill('最初の依頼');
    await check('below');

    // Sending starts a real conversation; its tall message leaves room above, so the list floats
    // over it and the window keeps its size.
    const lines = Array.from({ length: 12 }, (_, index) => `${index + 1}. 確認したいこと`);
    await input.fill(lines.join('\n'));
    await input.press('Enter');
    await expect(overlay.getByLabel('あなた')).toContainText('12. 確認したいこと');
    await expect(input).toHaveValue('');
    await expect(input).not.toHaveAttribute('readonly', '');
    await overlay.waitForTimeout(1000);
    await input.fill('続き');
    await check('above');
  } finally {
    await stop({ keepArtifacts: testInfo.status !== testInfo.expectedStatus });
  }
});
