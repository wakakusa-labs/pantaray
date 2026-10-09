import { type App, type BrowserWindow, type MenuItemConstructorOptions, type Tray } from 'electron';

import type { UiLanguage, UpdateReadyNotice } from '../ipc/context';
import { getTrayMenuCopy, getUpdateMenuCopy, type UpdateMenuCopy } from '../ui/mainProcessCopy';
import type { CaptureStatusSnapshot } from '../screenshot/captureStatus';
import type { RecordingStartResult } from '../screenshot/screenshotSync';
import type { TrayStatusVisual } from './trayStatusIcon';

export type { UpdateMenuCopy };
export type { TrayStatusVisual };

type DialogLike = {
  showMessageBox(options: {
    type: 'info';
    title: string;
    message: string;
    detail?: string;
    buttons?: string[];
    defaultId?: number;
  }): Promise<{ response: number }>;
};

type MenuLike = {
  buildFromTemplate(template: MenuItemConstructorOptions[]): Electron.Menu;
  setApplicationMenu(menu: Electron.Menu | null): void;
};

type DesktopUpdaterLike = {
  isUpdateDownloaded(): boolean;
  getPendingVersion(): string | null;
  getUpdateState(): string | null;
  checkForUpdates(reason: 'manual'): Promise<void> | void;
  quitAndInstall(): void;
};

type UpdateUiManagerDeps = {
  app: App;
  dialog: DialogLike;
  menu: MenuLike;
  getUiLanguage: () => UiLanguage;
  getDesktopUpdater: () => DesktopUpdaterLike | null;
  canCheckForUpdatesNow: () => boolean;
  markManualUpdateCheckPending: () => void;
  consumeManualUpdateCheckPending: () => boolean;
  getTray: () => Tray | null;
  getMainWindow: () => BrowserWindow | null;
  openNewConversationOverlay: () => void;
  /** Accelerator to display next to the tray entry; null while no global shortcut is active. */
  getGlobalShortcutAccelerator: () => string | null;
  getCaptureStatusSnapshot: () => Promise<CaptureStatusSnapshot | null>;
  startScreenshots: () => Promise<RecordingStartResult>;
  stopScreenshots: () => Promise<boolean>;
  setTrayStatusVisual: (visual: TrayStatusVisual) => void;
  onQuitRequested: () => void;
};

export function resolveTrayIconPath(params: {
  app: App;
  frontendRoot: string;
  relativeIconPath: string;
}): string {
  const { app, frontendRoot, relativeIconPath } = params;
  if (app.isPackaged) {
    return `${app.getAppPath()}/${relativeIconPath}`;
  }
  return `${frontendRoot}/${relativeIconPath}`;
}

export function getTrayStatusVisual(status: CaptureStatusSnapshot | null): TrayStatusVisual {
  return status?.screenshotsEnabled ? 'capturing' : 'idle';
}

function clearTrayStatusTitle(tray: Tray): void {
  const titledTray = tray as Tray & { setTitle?: (title: string) => void };
  titledTray.setTitle?.('');
}
export function createUpdateUiManager(deps: UpdateUiManagerDeps) {
  let captureStatusSnapshot: CaptureStatusSnapshot | null = null;
  let captureStatusRefreshSequence = 0;

  // One restart path for the menus, the update dialog and the main window's notice.
  const restartToUpdate = (): void => {
    try {
      const updater = deps.getDesktopUpdater();
      // The main window can ask at any time; only a downloaded update restarts the app.
      if (!updater?.isUpdateDownloaded()) return;
      deps.onQuitRequested();
      updater.quitAndInstall();
    } catch {
      // no-op
    }
  };

  const getReadyNotice = (): UpdateReadyNotice | null => {
    const updater = deps.getDesktopUpdater();
    if (!updater?.isUpdateDownloaded()) return null;
    return { version: updater.getPendingVersion() };
  };

  const handleCheckUpdatesClick = (text: UpdateMenuCopy): void => {
    try {
      const updater = deps.getDesktopUpdater();
      if (!updater) {
        deps.markManualUpdateCheckPending();
        return;
      }

      if (!deps.canCheckForUpdatesNow()) {
        void deps.dialog.showMessageBox({
          type: 'info',
          title: text.updateUnavailableTitle,
          message: text.updateUnavailableBody,
        });
        return;
      }

      if (updater.getUpdateState() === 'downloading') {
        void deps.dialog.showMessageBox({
          type: 'info',
          title: text.downloadingTitle,
          message: text.downloadingBody,
        });
        return;
      }

      void updater.checkForUpdates('manual');
    } catch {
      // no-op
    }
  };

  const handleNoUpdateAvailable = (text: UpdateMenuCopy): void => {
    try {
      const updater = deps.getDesktopUpdater();
      // Nothing newer than the update already prepared: a restart installs that one.
      if (updater?.isUpdateDownloaded()) {
        const pending = updater.getPendingVersion();
        const versionHint = pending ? ` (${deps.app.getVersion()} → ${pending})` : '';
        void deps.dialog
          .showMessageBox({
            type: 'info',
            title: text.updateReadyTitle,
            message: `${text.updateReadyBody}${versionHint}`,
            buttons: [text.restartToUpdate, text.laterLabel],
            defaultId: 0,
          })
          .then(({ response }) => {
            if (response === 0) restartToUpdate();
          });
        return;
      }
      void deps.dialog.showMessageBox({
        type: 'info',
        title: text.noUpdateTitle,
        message: `${text.noUpdateBody} (${deps.app.getVersion()})`,
      });
    } catch {
      // no-op
    }
  };

  const handleUpdateCheckFailed = (text: UpdateMenuCopy): void => {
    try {
      void deps.dialog.showMessageBox({
        type: 'info',
        title: text.checkFailedTitle,
        message: text.checkFailedBody,
      });
    } catch {
      // no-op
    }
  };

  const rebuildAppMenu = (): void => {
    try {
      if (process.platform !== 'darwin') return;

      const hasUpdateReady = Boolean(deps.getDesktopUpdater()?.isUpdateDownloaded());
      const text = getUpdateMenuCopy(deps.getUiLanguage());
      const updateMenuItems: MenuItemConstructorOptions[] = [
        {
          label: text.checkUpdates,
          click: () => handleCheckUpdatesClick(text),
        },
        ...(hasUpdateReady
          ? [
              {
                label: text.restartToUpdate,
                click: () => restartToUpdate(),
              } satisfies MenuItemConstructorOptions,
            ]
          : []),
      ];
      const menu = deps.menu.buildFromTemplate([
        {
          label: deps.app.name,
          submenu: [
            { role: 'about' },
            { type: 'separator' },
            ...updateMenuItems,
            { type: 'separator' },
            { role: 'services' },
            { type: 'separator' },
            { role: 'hide' },
            { role: 'hideOthers' },
            { role: 'unhide' },
            { type: 'separator' },
            { role: 'quit' },
          ],
        },
        { role: 'fileMenu' },
        { role: 'editMenu' },
        { role: 'viewMenu' },
        { role: 'windowMenu' },
        { role: 'help', submenu: [] },
      ]);

      deps.menu.setApplicationMenu(menu);
    } catch {
      // no-op
    }
  };

  const rebuildTrayMenu = (): void => {
    try {
      const tray = deps.getTray();
      if (!tray) return;

      const hasUpdateReady = Boolean(deps.getDesktopUpdater()?.isUpdateDownloaded());
      const updateText = getUpdateMenuCopy(deps.getUiLanguage());
      const text = getTrayMenuCopy(deps.getUiLanguage());
      const captureStatusText = text.captureStatus(captureStatusSnapshot);
      const screenshotsEnabled = Boolean(captureStatusSnapshot?.screenshotsEnabled);
      const newConversationAccelerator = deps.getGlobalShortcutAccelerator();

      const template: MenuItemConstructorOptions[] = [
        {
          label: captureStatusText.label,
          enabled: false,
        },
        ...(captureStatusText.detail
          ? [
              {
                label: captureStatusText.detail,
                enabled: false,
              } satisfies MenuItemConstructorOptions,
            ]
          : []),
        { type: 'separator' },
        {
          label: text.newConversation,
          click: deps.openNewConversationOverlay,
          // Display only: the global shortcut stays registered by globalShortcutController.
          ...(newConversationAccelerator
            ? { accelerator: newConversationAccelerator, registerAccelerator: false }
            : {}),
        },
        {
          label: text.openWindow,
          click: () => {
            try {
              const mainWindow = deps.getMainWindow();
              if (mainWindow && !mainWindow.isDestroyed()) {
                if (!mainWindow.isVisible()) mainWindow.show();
                mainWindow.focus();
              }
            } catch {
              // no-op
            }
          },
        },
        { type: 'separator' },
        {
          label: screenshotsEnabled ? text.stopScreenshots : text.startScreenshots,
          click: async () => {
            try {
              if (screenshotsEnabled) {
                await deps.stopScreenshots();
              } else {
                await deps.startScreenshots();
              }
              void refreshCaptureStatus();
            } catch {
              // no-op
            }
          },
        },
        { type: 'separator' },
        {
          label: updateText.checkUpdates,
          click: () => handleCheckUpdatesClick(updateText),
        },
        ...(hasUpdateReady
          ? [
              {
                label: updateText.restartToUpdate,
                click: () => restartToUpdate(),
              } satisfies MenuItemConstructorOptions,
            ]
          : []),
        { type: 'separator' },
        {
          label: text.quit,
          click: () => {
            try {
              deps.app.quit();
            } catch {
              // no-op
            }
          },
        },
      ];
      tray.setContextMenu(deps.menu.buildFromTemplate(template));
      tray.setToolTip(text.tip(hasUpdateReady, captureStatusSnapshot));
      clearTrayStatusTitle(tray);
      deps.setTrayStatusVisual(getTrayStatusVisual(captureStatusSnapshot));
    } catch {
      // no-op
    }
  };

  const handleUpdateDownloaded = (): void => {
    rebuildTrayMenu();
    rebuildAppMenu();
    const mainWindow = deps.getMainWindow();
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('update:readyNoticeChanged');
    }
  };

  const refreshCaptureStatus = async (): Promise<void> => {
    const refreshSequence = captureStatusRefreshSequence + 1;
    captureStatusRefreshSequence = refreshSequence;
    let nextSnapshot: CaptureStatusSnapshot | null = null;
    try {
      nextSnapshot = await deps.getCaptureStatusSnapshot();
    } catch {
      nextSnapshot = null;
    }
    if (refreshSequence !== captureStatusRefreshSequence) return;
    captureStatusSnapshot = nextSnapshot;
    rebuildTrayMenu();
  };

  return {
    consumeManualUpdateCheckPending: deps.consumeManualUpdateCheckPending,
    getReadyNotice,
    getUpdateMenuCopy,
    handleCheckUpdatesClick,
    handleNoUpdateAvailable,
    handleUpdateCheckFailed,
    handleUpdateDownloaded,
    restartToUpdate,
    refreshCaptureStatus,
    rebuildAppMenu,
    rebuildTrayMenu,
  };
}
