import {
  CallIdSchema,
  MeetingSettingsSchema,
  type MeetingCommandResult,
} from '../../context/meetingTranscript';
import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import { parseInput } from '../schemas/error';
import { runAuditedMutation } from './auditedMutation';

/** Call transcription: the call the recorder offers, the user's answer, and the settings. */
export function registerMeetingHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  registrar.handle('meeting:getCall', () => ctx.meeting.getCall());
  registrar.handle('meeting:getAvailability', () => ctx.meeting.readAvailability());
  registrar.handle('meeting:getSettings', () => ctx.meeting.getSettings());

  const noRecorder = (result: MeetingCommandResult) => result === 'no_recorder';
  for (const command of ['record', 'stop'] as const) {
    const channel = `meeting:${command}` as const;
    registrar.handle(channel, (_evt, callId) => {
      const parsed = parseInput(CallIdSchema, channel, callId);
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
  registrar.handle('meeting:decline', (_evt, callId) =>
    ctx.meeting.decline(parseInput(CallIdSchema, 'meeting:decline', callId))
  );

  registrar.handle('meeting:updateSettings', (_evt, settings) => {
    const parsed = parseInput(MeetingSettingsSchema, 'meeting:updateSettings', settings);
    return runAuditedMutation(ctx, 'meeting.updateSettings', () =>
      ctx.meeting.updateSettings(parsed)
    );
  });
}
