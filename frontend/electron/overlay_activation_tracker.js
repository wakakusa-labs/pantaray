const DEFAULT_RECENT_INTERACTION_WINDOW_MS = 250;

function isPointInsideBounds(point, bounds) {
  if (!point || !bounds) return false;
  const x = Number(point.x);
  const y = Number(point.y);
  const left = Number(bounds.x);
  const top = Number(bounds.y);
  const width = Number(bounds.width);
  const height = Number(bounds.height);
  if (![x, y, left, top, width, height].every(Number.isFinite)) return false;
  return x >= left && x < left + width && y >= top && y < top + height;
}

function isVisibleWindowAtPoint(win, point) {
  if (!win || (typeof win.isDestroyed === 'function' && win.isDestroyed())) return false;
  try {
    if (typeof win.isVisible === 'function' && !win.isVisible()) return false;
    if (typeof win.getBounds !== 'function') return false;
    return isPointInsideBounds(point, win.getBounds());
  } catch {
    return false;
  }
}

function createOverlayActivationTracker(options = {}) {
  const now = typeof options.now === 'function' ? options.now : () => Date.now();
  const overlayWindows = new Set();
  let lastInteractionAtMs = 0;
  let activeInteractionCount = 0;

  return {
    registerOverlayWindow(win) {
      if (win) overlayWindows.add(win);
    },
    unregisterOverlayWindow(win) {
      if (win) overlayWindows.delete(win);
    },
    isOverlayWindow(win) {
      return Boolean(win && overlayWindows.has(win));
    },
    isVisibleOverlayAtPoint(point) {
      for (const win of overlayWindows) {
        if (isVisibleWindowAtPoint(win, point)) return true;
      }
      return false;
    },
    recordInteraction() {
      lastInteractionAtMs = now();
    },
    beginInteraction() {
      activeInteractionCount += 1;
      lastInteractionAtMs = now();
    },
    endInteraction() {
      activeInteractionCount = Math.max(0, activeInteractionCount - 1);
      lastInteractionAtMs = now();
    },
    hasRecentInteraction(referenceMs = now()) {
      if (activeInteractionCount > 0) return true;
      return (
        lastInteractionAtMs > 0 &&
        referenceMs - lastInteractionAtMs <= DEFAULT_RECENT_INTERACTION_WINDOW_MS
      );
    },
  };
}

module.exports = {
  createOverlayActivationTracker,
};
