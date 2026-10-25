/**
 * screenshot:* IPC handlers
 *
 * 目的:
 * - スクリーンショット同期の ON/OFF を renderer から制御できるようにする。
 * - 実際の制御（WS接続状態/プラットフォーム制約など）は main の SSOT に委譲する。
 */

import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import { parseInput } from '../schemas/error';
import { CapturePermissionPaneSchema } from '../schemas/onboarding';
import { IdSchema } from '../schemas/workspaceSettings';
import { runAuditedMutation } from './auditedMutation';

export function registerScreenshotHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  registrar.handle('screenshot:getStatus', () => ctx.screenshot.getStatus());

  registrar.handle('screenshot:start', () =>
    runAuditedMutation(
      ctx,
      'screenshot.start',
      () => ctx.screenshot.start(),
      (result) => result === 'failed'
    )
  );

  registrar.handle('screenshot:stop', () =>
    runAuditedMutation(ctx, 'screenshot.stop', () => ctx.screenshot.stop())
  );

  registrar.handle('recording:getGateState', () => ctx.screenshot.getGateState());

  // Neither of these changes anything durable: "later" only drops the conversation
  // main was holding, and the settings pane is opened for the user to grant in.
  registrar.handle('recording:dismissIntro', (_evt, ownerId) =>
    ctx.screenshot.dismissIntro(parseInput(IdSchema, 'recording:dismissIntro', ownerId))
  );

  registrar.handle('recording:askForPermission', (_evt, permission) =>
    ctx.screenshot.askForPermission(
      parseInput(CapturePermissionPaneSchema, 'recording:askForPermission', permission)
    )
  );
}
