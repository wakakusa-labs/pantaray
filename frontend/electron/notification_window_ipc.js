const OVERLAY_RESIZE_TOP_MARGIN_PX = 8;
const OVERLAY_RESIZE_BOTTOM_MARGIN_PX = 8;
const OVERLAY_MIN_HEIGHT_PX = 120;

function clampOverlayBounds(screen, win, requestedHeight) {
  if (!win || win.isDestroyed()) return null;
  const bounds = win.getBounds();
  const display = screen.getDisplayMatching(bounds) || screen.getPrimaryDisplay();
  const workArea = display?.workArea || screen.getPrimaryDisplay().workArea;
  const maximumHeight = Math.max(
    OVERLAY_MIN_HEIGHT_PX,
    workArea.height - OVERLAY_RESIZE_TOP_MARGIN_PX - OVERLAY_RESIZE_BOTTOM_MARGIN_PX
  );
  const requested = Number(requestedHeight) || 0;
  const height = Math.max(OVERLAY_MIN_HEIGHT_PX, Math.min(requested, maximumHeight));
  const minimumY = workArea.y + OVERLAY_RESIZE_TOP_MARGIN_PX;
  const maximumY = workArea.y + workArea.height - OVERLAY_RESIZE_BOTTOM_MARGIN_PX - height;
  const y = Math.max(minimumY, Math.min(bounds.y, maximumY));
  return { previousY: bounds.y, height, y };
}

function resizeWindow(screen, win, requestedHeight) {
  const nextBounds = clampOverlayBounds(screen, win, requestedHeight);
  if (nextBounds) win.setBounds({ y: nextBounds.y, height: nextBounds.height });
}

function findSenderWindow(BrowserWindow, event) {
  try {
    return event?.sender ? BrowserWindow.fromWebContents(event.sender) : null;
  } catch {
    return null;
  }
}

function hideSenderWindow(BrowserWindow, windows, event) {
  const win = findSenderWindow(BrowserWindow, event);
  if (!win || win.isDestroyed()) return false;
  const overlayId = windows.findOverlayIdByWindow(win);
  if (overlayId) windows.hide(overlayId);
  else win.hide();
  return true;
}

function hideLastWindow(windows) {
  const lastOverlayId = windows.getLastOverlayId();
  if (lastOverlayId) windows.hide(lastOverlayId);
}

function createNotificationIpcHandlerFactory({ BrowserWindow, screen, windows, interactions }) {
  return function createNotificationIpcHandlers(options = {}) {
    const { refreshActionConversation, resumeLiveProcess, resolveOverlayBootstrap } = options;
    windows.setMainWindowGetter(
      typeof options.getMainWindow === 'function' ? options.getMainWindow : null
    );

    return {
      onResizeNotificationWindow: (event, payload) => {
        const targetId = payload && typeof payload === 'object' ? payload.id : null;
        const requestedHeight = payload && typeof payload === 'object' ? payload.height : payload;
        if (targetId) {
          resizeWindow(screen, windows.getOverlay(String(targetId)), requestedHeight);
          return;
        }
        resizeWindow(screen, findSenderWindow(BrowserWindow, event), requestedHeight);
      },
      onNotificationActionAccept: (_event, data) => {
        console.log('Notification Accepted:', data);
      },
      onNotificationActionReject: (event, data) => {
        const suggestionId = data?.suggestion_id ? String(data.suggestion_id) : null;
        if (suggestionId) {
          windows.hide(suggestionId);
          return;
        }
        if (!hideSenderWindow(BrowserWindow, windows, event)) hideLastWindow(windows);
      },
      onNotificationHide: (event) => {
        if (!hideSenderWindow(BrowserWindow, windows, event)) hideLastWindow(windows);
      },
      onNotificationStopAction: () => {},
      onOverlayInteraction: (event) => {
        const win = findSenderWindow(BrowserWindow, event);
        if (!interactions.activationTracker.isOverlayWindow(win)) return;
        interactions.activationTracker.recordInteraction();
      },
      onOverlayDragStart: (event, payload) => interactions.dragController.start(event, payload),
      onOverlayDragMove: (event, payload) => interactions.dragController.move(event, payload),
      onOverlayDragEnd: (event) => interactions.dragController.end(event),
      onHistoryOpenOverlay: (_event, payload) => {
        void (async () => {
          if (!payload || typeof payload !== 'object') return;
          const suggestionId = windows.normalizeId(payload.suggestionId || payload.id);
          if (!suggestionId) return;
          if (typeof resolveOverlayBootstrap !== 'function') {
            throw new Error('resolveOverlayBootstrap is unavailable');
          }
          const ownerScope = windows.readOwnerScope();
          const bootstrap = await resolveOverlayBootstrap(suggestionId);
          if (!windows.isOwnerScopeCurrent(ownerScope) || !bootstrap) return;

          windows.setSnapshot(suggestionId, {
            snapshot: bootstrap.snapshot,
            initialUiState:
              payload.initialUiState && typeof payload.initialUiState === 'object'
                ? payload.initialUiState
                : null,
          });
          windows.showHistory(suggestionId);

          const actionId = windows.normalizeId(bootstrap.snapshot.actionId);
          if (actionId) {
            if (typeof refreshActionConversation !== 'function') {
              throw new Error('refreshActionConversation is unavailable');
            }
            refreshActionConversation(actionId);
          }

          const liveResume = bootstrap.liveResume;
          if (
            liveResume.kind !== 'none' &&
            liveResume.processId &&
            typeof resumeLiveProcess === 'function'
          ) {
            resumeLiveProcess({
              kind: liveResume.kind,
              suggestionId,
              actionId: liveResume.actionId,
              commandId: liveResume.commandId,
              processId: liveResume.processId,
              fromStart: payload.fromStart !== false,
            });
          }
        })().catch((error) => {
          console.error('history:openOverlay failed:', error);
        });
      },
    };
  };
}

module.exports = { createNotificationIpcHandlerFactory };
