import {
  CallIdSchema,
  MeetingSettingsSchema,
  type MeetingCommandResult,
} from '../../context/meetingTranscript';
import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import { parseInput } from '../schemas/error';
import { IpcSenderRejectedError, type IpcSenderIdentity } from '../senderTrust';
import { runAuditedMutation } from './auditedMutation';

/**
 * Who may answer for a call. The main window's controls are the user's own clicks. An Overlay
 * answers only as the consent chip main opened for this call, while that call is still asked
 * about: any other panel, another call or an expired consent is refused.
 */
function requireAnswerer(ctx: MainContext, sender: IpcSenderIdentity, callId: string): void {
  const mainWindow = ctx.windows.getMainWindow();
  if (mainWindow !== null && !mainWindow.isDestroyed() && mainWindow.webContents === sender) return;
  const call = ctx.meeting.getCall();
  if (
    !ctx.meeting.isConsentChip(sender, callId) ||
    call.kind !== 'awaiting_consent' ||
    call.call_id !== callId ||
    Date.parse(call.expires_at) <= Date.now()
  )
    throw new IpcSenderRejectedError('meeting_consent_not_open');
}

/** Call transcription: the call the recorder offers, the user's answer, and the settings. */
export function registerMeetingHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  registrar.handle('meeting:getCall', () => ctx.meeting.getCall());
  registrar.handle('meeting:getAvailability', () => ctx.meeting.readAvailability());
  registrar.handle('meeting:getSettings', () => ctx.meeting.getSettings());
  registrar.handle('meeting:getDefaultCallLists', () => ctx.meeting.readDefaultCallLists());

  const noRecorder = (result: MeetingCommandResult) => result === 'no_recorder';
  for (const command of ['record', 'stop'] as const) {
    const channel = `meeting:${command}` as const;
    registrar.handle(channel, (event, callId) => {
      const parsed = parseInput(CallIdSchema, channel, callId);
      if (command === 'record') requireAnswerer(ctx, event.sender, parsed);
      return runAuditedMutation(
        ctx,
        `meeting.${command}`,
        () => ctx.meeting[command](parsed),
        noRecorder
      );
    });
  }
  registrar.handle('meeting:prepare', () =>
    runAuditedMutation(ctx, 'meeting.prepare', () => ctx.meeting.prepare(), noRecorder)
  );

  // Changes only what this app shows: the recorder still closes the consent at its deadline.
  registrar.handle('meeting:decline', (event, callId) => {
    const parsed = parseInput(CallIdSchema, 'meeting:decline', callId);
    requireAnswerer(ctx, event.sender, parsed);
    ctx.meeting.decline(parsed);
  });

  registrar.handle('meeting:updateSettings', (_evt, settings) => {
    const parsed = parseInput(MeetingSettingsSchema, 'meeting:updateSettings', settings);
    return runAuditedMutation(ctx, 'meeting.updateSettings', () =>
      ctx.meeting.updateSettings(parsed)
    );
  });
}
