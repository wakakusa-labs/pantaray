import path from 'path';
import { Tray, nativeImage, type App, type BrowserWindow } from 'electron';

import { resolveTrayIconPath } from './updateUi';
import { createTrayStatusIcon, type TrayStatusVisual } from './trayStatusIcon';

type LoggerLike = {
  info?: (name: string, payload?: unknown) => void;
  warn?: (name: string, payload?: unknown) => void;
};

const TRAY_STATUS_ICON_RELATIVE_PATHS: Record<TrayStatusVisual, string> = {
  capturing: path.join('assets', 'icons', 'Pantaray_tray_capturing.png'),
  idle: path.join('assets', 'icons', 'Pantaray_tray_idle.png'),
  transcribing: path.join('assets', 'icons', 'Pantaray_tray_transcribing.png'),
};
const TRAY_STATUS_RETINA_SUFFIX = '@2x';

export function createTrayController(params: {
  app: App;
  frontendRoot: string;
  getMainWindow: () => BrowserWindow | null;
  logger: LoggerLike | null;
  rebuildAppMenu: () => void;
  rebuildTrayMenu: () => void;
  refreshCaptureStatus: () => Promise<void>;
}) {
  let tray: Tray | null = null;

  const resolveStatusIconPath = (visual: TrayStatusVisual): string =>
    resolveTrayIconPath({
      app: params.app,
      frontendRoot: params.frontendRoot,
      relativeIconPath: TRAY_STATUS_ICON_RELATIVE_PATHS[visual],
    });

  const resolveRetinaStatusIconPath = (visual: TrayStatusVisual): string => {
    const iconPath = resolveStatusIconPath(visual);
    const extension = path.extname(iconPath);
    return `${iconPath.slice(0, -extension.length)}${TRAY_STATUS_RETINA_SUFFIX}${extension}`;
  };

  const buildStatusIcon = (visual: TrayStatusVisual): Electron.NativeImage =>
    createTrayStatusIcon({
      iconPath: resolveStatusIconPath(visual),
      nativeImage,
      retinaIconPath: resolveRetinaStatusIconPath(visual),
      visual,
    });

  const refreshMenusAndStatus = (): void => {
    params.rebuildTrayMenu();
    void params.refreshCaptureStatus();
    params.rebuildAppMenu();
  };

  const focusMainWindow = (): void => {
    try {
      const mainWindow = params.getMainWindow();
      if (!mainWindow || mainWindow.isDestroyed()) return;
      if (!mainWindow.isVisible()) mainWindow.show();
      mainWindow.focus();
    } catch {
      // no-op
    }
  };

  return {
    getTray: () => tray,
    initialize: (): void => {
      if (tray) {
        refreshMenusAndStatus();
        return;
      }
      try {
        const iconPath = resolveStatusIconPath('idle');
        const icon = buildStatusIcon('idle');
        tray = new Tray(icon);
        tray.on('click', focusMainWindow);
        refreshMenusAndStatus();
        params.logger?.info?.('TRAY_INIT_OK', { iconPath, iconLoaded: !icon.isEmpty() });
      } catch (error) {
        params.logger?.warn?.('TRAY_INIT_ERR', { err: error });
      }
    },
    setStatusVisual: (visual: TrayStatusVisual): void => {
      try {
        tray?.setImage(buildStatusIcon(visual));
      } catch (error) {
        params.logger?.warn?.('TRAY_STATUS_ICON_ERR', { err: error });
      }
    },
  };
}
