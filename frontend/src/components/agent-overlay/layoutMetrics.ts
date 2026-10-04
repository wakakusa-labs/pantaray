/** The collapsed preview shows two lines of body text, whatever the overlay's line height. */
export const COLLAPSED_PREVIEW_LINES = 2;
export const COLLAPSED_PREVIEW_EXTRA_PX = 12;
export const COLLAPSED_PREVIEW_MAX_HEIGHT_CSS = `calc(${COLLAPSED_PREVIEW_LINES}lh + ${COLLAPSED_PREVIEW_EXTRA_PX}px)`;

export type ScrollableExpansionMetrics = {
  contentHeightPx: number;
  collapsedPreviewHeightPx: number;
  thresholdPx: number;
};

export function getCollapsedPreviewHeightPx(lineHeightPx: number): number {
  if (!Number.isFinite(lineHeightPx) || lineHeightPx <= 0) {
    return COLLAPSED_PREVIEW_EXTRA_PX;
  }
  return lineHeightPx * COLLAPSED_PREVIEW_LINES + COLLAPSED_PREVIEW_EXTRA_PX;
}

export function shouldExpandScrollableContent({
  contentHeightPx,
  collapsedPreviewHeightPx,
  thresholdPx,
}: ScrollableExpansionMetrics): boolean {
  if (contentHeightPx <= 0 || collapsedPreviewHeightPx <= 0) {
    return false;
  }
  return contentHeightPx > collapsedPreviewHeightPx + thresholdPx;
}
