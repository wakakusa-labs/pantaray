import { app, type Dialog } from 'electron';

import type { UiLanguage } from '../ipc/context';
import type { UpdateMenuCopy } from '../main_runtime/updateUi';
import { createDesktopUpdater, type DesktopUpdater } from './desktopUpdater';
import { markUpdateInstallQuitRequested } from './updateInstallLifecycle';

type LoggerLike = {
  info?: (name: string, payload?: unknown) => void;
  warn?: (name: string, payload?: unknown) => void;
  error?: (name: string, payload?: unknown) => void;
};

type UpdateUiLike = {
  getUpdateMenuCopy(language: UiLanguage): UpdateMenuCopy;
  handleUpdateDownloaded(): void;
  handleNoUpdateAvailable(text: UpdateMenuCopy): void;
  handleUpdateCheckFailed(text: UpdateMenuCopy): void;
};

export function createDesktopUpdaterForMain(params: {
  getUiLanguage: () => UiLanguage;
  updateUi: UpdateUiLike;
  dialog: Pick<Dialog, 'showMessageBox'>;
  logger: LoggerLike | null;
}): DesktopUpdater {
  return createDesktopUpdater({
    logger: params.logger,
    beforeQuitAndInstall: () => markUpdateInstallQuitRequested(app),
    onUpdateAvailable: ({ reason }) => {
      if (reason !== 'manual') return;
      const text = params.updateUi.getUpdateMenuCopy(params.getUiLanguage());
      void params.dialog.showMessageBox({
        type: 'info',
        title: text.downloadingTitle,
        message: text.downloadingBody,
      });
    },
    onReadyChanged: () => params.updateUi.handleUpdateDownloaded(),
    onUpdateNotAvailable: ({ reason }) => {
      if (reason !== 'manual') return;
      params.updateUi.handleNoUpdateAvailable(
        params.updateUi.getUpdateMenuCopy(params.getUiLanguage())
      );
    },
    onUpdateError: ({ reason }) => {
      if (reason !== 'manual') return;
      params.updateUi.handleUpdateCheckFailed(
        params.updateUi.getUpdateMenuCopy(params.getUiLanguage())
      );
    },
  });
}
