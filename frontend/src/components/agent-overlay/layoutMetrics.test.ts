import { describe, expect, it } from 'vitest';
import { getCollapsedPreviewHeightPx, shouldExpandScrollableContent } from './layoutMetrics';

describe('layoutMetrics', () => {
  it('calculates the collapsed preview height from the scroll container line height', () => {
    expect(getCollapsedPreviewHeightPx(26.25)).toBeCloseTo(64.5);
  });

  it('expands only when full content exceeds the collapsed preview threshold', () => {
    expect(
      shouldExpandScrollableContent({
        contentHeightPx: 84,
        collapsedPreviewHeightPx: 80,
        thresholdPx: 4,
      })
    ).toBe(false);

    expect(
      shouldExpandScrollableContent({
        contentHeightPx: 85,
        collapsedPreviewHeightPx: 80,
        thresholdPx: 4,
      })
    ).toBe(true);
  });
});
