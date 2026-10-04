/**
 * privacy:* IPC handlers
 *
 * Mutating channels validate their payload against an explicit schema at
 * the IPC boundary. Invalid input rejects the invoke Promise with
 * `IpcValidationError` rather than silently rolling back. Read-only
 * channels keep narrow `try/catch → null` blocks because their OS-probe
 * implementations are a known transient failure source.
 */

import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import { parseInput } from '../schemas/error';
import {
  CaptureEditingSchema,
  CapturePrivacySettingsSchema,
  IdeFileRulesSchema,
} from '../schemas/privacy';
import { runAuditedMutation } from './auditedMutation';

export function registerPrivacyHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  registrar.handle('privacy:getCaptureSettings', () => ctx.privacy.getCaptureSettings());

  registrar.handle('privacy:updateCaptureSettings', (_evt, next) => {
    const parsed = parseInput(CapturePrivacySettingsSchema, 'privacy:updateCaptureSettings', next);
    return runAuditedMutation(ctx, 'privacy.updateCaptureSettings', () =>
      ctx.privacy.updateCaptureSettings(parsed)
    );
  });

  registrar.handle('privacy:listInstalledApps', () => ctx.privacy.listInstalledApps());

  registrar.handle('privacy:getIdeFileRules', () => ctx.privacy.getIdeFileRules());

  registrar.handle('privacy:setCaptureEditing', (_evt, request) => {
    const parsed = parseInput(CaptureEditingSchema, 'privacy:setCaptureEditing', request);
    return runAuditedMutation(ctx, 'privacy.setCaptureEditing', () =>
      ctx.privacy.setCaptureEditing(parsed)
    );
  });

  registrar.handle('privacy:updateIdeFileRules', (_evt, nextRules) => {
    const parsed = parseInput(IdeFileRulesSchema, 'privacy:updateIdeFileRules', nextRules);
    return runAuditedMutation(ctx, 'privacy.updateIdeFileRules', () =>
      ctx.privacy.updateIdeFileRules(parsed)
    );
  });
}
