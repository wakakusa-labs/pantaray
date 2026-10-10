import type { Page } from '@playwright/test';

/**
 * Adds the shared task draft calls that preload gives every window to the bridge a spec
 * installed: one page's drafts, kept in memory as main keeps them. Register it after the
 * spec's own bridge, since init scripts run in order.
 */
export async function installTaskDrafts(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const drafts = new Map<string, unknown>();
    const actions = (window as unknown as { electron: { actions: Record<string, unknown> } })
      .electron.actions;
    Object.assign(actions, {
      openDraft: async ({ work }: { work: string }) => drafts.get(work) ?? null,
      updateDraft: async ({
        work,
        draft,
      }: {
        work: string;
        draft: { text: string; attachments: unknown[]; retry: unknown };
      }) => {
        const empty = draft.text === '' && draft.attachments.length === 0 && draft.retry === null;
        if (empty) drafts.delete(work);
        else drafts.set(work, draft);
      },
      closeDraft: async () => undefined,
      // A page is one window: no other window changes its drafts.
      onDraftChanged: () => () => undefined,
    });
  });
}
