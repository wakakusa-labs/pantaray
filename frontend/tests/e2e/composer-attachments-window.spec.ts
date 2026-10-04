import fs from 'node:fs';
import path from 'node:path';
import { test, expect } from '@playwright/test';
import { launchElectronE2E } from './harness';

const PDF_BYTES = Buffer.from('%PDF-1.4\n%%EOF\n');

function findFiles(root: string, name: string): string[] {
  if (!fs.existsSync(root)) return [];
  return (fs.readdirSync(root, { recursive: true }) as string[])
    .filter((relative) => path.basename(relative) === name)
    .map((relative) => path.join(root, relative));
}

// eslint-disable-next-line no-empty-pattern
test('a document picked in the composer reaches the Action workspace when sent', async ({}, testInfo) => {
  const { harness, stop } = await launchElectronE2E();
  try {
    const { app, page, localArtifactRoot } = harness;
    await page.waitForURL(/#\/history$/);
    // The overlay opens only once the local runtime is up.
    await expect
      .poll(() =>
        page.evaluate(() =>
          window.electron!.workspaceSettings!.get().then(
            () => 'ready',
            (error: Error) => error.message
          )
        )
      )
      .toBe('ready');
    await page.evaluate(() => window.electron!.history!.openNewConversation());
    await expect
      .poll(() => app.windows().some((w) => w.url().includes('mode=standalone')))
      .toBe(true);
    const overlay = app.windows().find((w) => w.url().includes('mode=standalone'))!;
    const input = overlay.getByRole('textbox', { name: 'メッセージ', exact: true });
    await expect(input).toBeVisible();

    await overlay.locator('.overlay-composer input[type="file"]').setInputFiles({
      name: '議事録.pdf',
      mimeType: 'application/pdf',
      buffer: PDF_BYTES,
    });
    await expect(overlay.getByRole('button', { name: '議事録.pdf を削除' })).toBeVisible();
    const stagingRoot = path.join(localArtifactRoot, 'generated', 'attachments');
    expect(
      fs.readdirSync(stagingRoot, { recursive: true }).some((p) => String(p).endsWith('.pdf'))
    ).toBe(true);

    await input.fill('この議事録を読んで');
    await input.press('Enter');
    const sent = overlay.getByRole('article', { name: 'あなた' });
    await expect(sent.getByRole('list', { name: '添付ファイル 1 件' })).toContainText('議事録.pdf');
    await expect(input).toHaveValue('');

    // The backend links the staged file into the Action workspace and removes the staged copy.
    const workspaces = path.join(path.dirname(localArtifactRoot), 'local_runtime_workspaces');
    await expect.poll(() => findFiles(workspaces, '議事録.pdf').length).toBe(1);
    const [linked] = findFiles(workspaces, '議事録.pdf');
    expect(linked).toMatch(/[/\\]attachments[/\\][0-9a-f-]{36}[/\\]議事録\.pdf$/);
    expect(fs.readFileSync(linked)).toEqual(PDF_BYTES);
    await expect
      .poll(() =>
        fs.readdirSync(stagingRoot, { recursive: true }).some((p) => String(p).endsWith('.pdf'))
      )
      .toBe(false);
  } finally {
    await stop({ keepArtifacts: testInfo.status !== testInfo.expectedStatus });
  }
});
