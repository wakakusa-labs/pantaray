/**
 * IPC 登録の集約点（main 側）
 *
 * 目的:
 * - IPC の登録をここに集約し、`main_legacy.js` の巨大化を止める。
 * - ドメイン別 handler へ責務を分割し、段階的に TS へ移設する足場を作る。
 */

import type { MainContext } from './context';
import type { RegisteredIpcChannels } from './registrar';
import { createIpcRegistrar } from './registrar';

import { registerAiConnectionHandlers } from './handlers/aiConnection';
import { registerAuthHandlers } from './handlers/auth';
import { registerApprovalHandlers } from './handlers/approval';
import { registerActionFileHandlers } from './handlers/actionFiles';
import { registerActionHandlers } from './handlers/actions';
import { registerActionImageHandlers } from './handlers/actionImages';
import { registerActionAttachmentHandlers } from './handlers/actionAttachments';
import { registerExternalUrlHandlers } from './handlers/externalUrl';
import { registerHistoryHandlers } from './handlers/history';
import { registerOverlayHandlers } from './handlers/overlay';
import { registerPrivacyHandlers } from './handlers/privacy';
import { registerScreenshotHandlers } from './handlers/screenshot';
import { registerShortcutHandlers } from './handlers/shortcut';
import { registerUiLanguageHandlers } from './handlers/uiLanguage';
import { registerUpdateHandlers } from './handlers/update';
import { registerWindowHandlers } from './handlers/window';
import { registerWorkspaceSettingsHandlers } from './handlers/workspaceSettings';
import { registerWsBridgeHandlers } from './handlers/wsBridge';

export type RegisterAllResult = {
  registered: RegisteredIpcChannels;
  dispose: () => void;
};

export function registerAllIpcHandlers(ctx: MainContext): RegisterAllResult {
  const registrar = createIpcRegistrar(ctx.ipcMain, ctx.security);

  registerAiConnectionHandlers(ctx, registrar);

  // invoke handlers
  registerWindowHandlers(ctx, registrar);
  registerAuthHandlers(ctx, registrar);
  registerApprovalHandlers(ctx, registrar);
  registerShortcutHandlers(ctx, registrar);
  registerWorkspaceSettingsHandlers(ctx, registrar);
  registerActionFileHandlers(ctx, registrar);
  registerActionHandlers(ctx, registrar);
  registerActionImageHandlers(ctx, registrar);
  registerActionAttachmentHandlers(ctx, registrar);
  registerHistoryHandlers(ctx, registrar);
  registerUiLanguageHandlers(ctx, registrar);
  registerUpdateHandlers(ctx, registrar);
  registerScreenshotHandlers(ctx, registrar);
  registerPrivacyHandlers(ctx, registrar);

  // send handlers
  registerExternalUrlHandlers(ctx, registrar);
  registerWsBridgeHandlers(ctx, registrar);
  registerOverlayHandlers(ctx, registrar);

  return {
    registered: registrar.getRegistered(),
    dispose: registrar.dispose,
  };
}
