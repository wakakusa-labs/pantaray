import type { Page } from '@playwright/test';

/**
 * Resolves once fonts are loaded and no finite animation is still running.
 *
 * A CSS transition interrupted by a newer style change is cancelled: its `finished` rejects
 * with AbortError and a reversing transition replaces it. The overlay does this on load: it
 * collapses on the first, empty measure and expands again when the conversation arrives,
 * which can land inside the collapse's margin transition. A cancelled animation no longer
 * paints, so it counts as settled, and the page is checked again for the replacement.
 */
export async function waitForAnimationsToSettle(page: Page): Promise<void> {
  await page.evaluate(async () => {
    await document.fonts.ready;
    const unsettled = () =>
      document
        .getAnimations()
        .filter(
          (animation) =>
            animation.playState !== 'finished' &&
            animation.effect?.getComputedTiming().iterations !== Infinity
        );
    for (let pending = unsettled(); pending.length > 0; pending = unsettled()) {
      await Promise.allSettled(pending.map((animation) => animation.finished));
    }
  });
}
