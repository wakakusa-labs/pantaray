import { test, expect } from '@playwright/test';
import { launchElectronE2E } from './harness';

// eslint-disable-next-line no-empty-pattern
test('the History chat sends to the local backend and reads its messages back', async ({}, testInfo) => {
  const { harness, stop } = await launchElectronE2E();
  try {
    const { page } = harness;
    await page.waitForURL(/#\/history$/);
    // The chat is read from the local runtime, which can come up after the page.
    await expect
      .poll(() =>
        page.evaluate(() =>
          window.electron!.chat!.listItems({ before: null, limit: 50 }).then(
            () => 'ready',
            (error: Error) => error.message
          )
        )
      )
      .toBe('ready');
    await page.reload();

    const chat = page.getByRole('list', { name: 'Pantaray とのチャット' });
    const input = page.getByRole('textbox', { name: 'メッセージ' });
    await input.fill('最初のメッセージ');
    await input.press('Enter');
    await expect(chat.getByRole('article', { name: 'あなた' })).toHaveCount(1);
    await expect(input).toHaveValue('');

    // A quoted reply with a document goes through the staging IPC and the backend's checks.
    const first = chat.getByRole('article', { name: 'あなた' }).first();
    await first.hover();
    await first.getByRole('button', { name: '引用して返信' }).click();
    await page
      .getByRole('form', { name: 'Pantaray へのメッセージ' })
      .locator('input[type="file"]')
      .setInputFiles({
        name: '議事録.pdf',
        mimeType: 'application/pdf',
        buffer: Buffer.from('%PDF-1.4\n%%EOF\n'),
      });
    await expect(page.getByRole('button', { name: '議事録.pdf を削除' })).toBeVisible();
    await input.fill('これを読んでおいて');
    await input.press('Enter');
    const second = chat.getByRole('article', { name: 'あなた' }).nth(1);
    await expect(second).toContainText('最初のメッセージ');
    await expect(second.getByRole('list', { name: '添付ファイル 1 件' })).toContainText(
      '議事録.pdf'
    );

    // Both messages are the backend's: a reload reads them back in order, quote and file included.
    await page.reload();
    await expect(chat.getByRole('article', { name: 'あなた' })).toHaveCount(2);
    const page1 = await page.evaluate(() =>
      window.electron!.chat!.listItems({ before: null, limit: 50 })
    );
    // Each message also starts a turn, whose reply or failure lands between them.
    const [latest, earliest] = page1.items.filter((item) => item.content.kind === 'user_message');
    expect(earliest.content).toMatchObject({ kind: 'user_message', text: '最初のメッセージ' });
    expect(latest.content).toMatchObject({
      kind: 'user_message',
      text: 'これを読んでおいて',
      quote_item_id: earliest.item_id,
      files: [{ name: '議事録.pdf' }],
    });
  } finally {
    await stop({ keepArtifacts: testInfo.status !== testInfo.expectedStatus });
  }
});
