import fs from 'node:fs';
import { z } from 'zod';
import { readZaneiManifest, recorderTranscribesCalls } from './zaneiConfig';

/**
 * Call transcription as the host of a recorder started with `--host-events`: the recorder
 * detects and transcribes calls, this app owns the consent and the stop. Its stdout events are
 * the only input, and its next start replays what is current.
 */

/** `call_` and a ULID, as the recorder writes it. */
export const CallIdSchema = z.string().regex(/^call_[0-7][0-9A-HJKMNP-TV-Z]{25}$/);
const CallAppSchema = z.object({ name: z.string(), bundle_id: z.string() });
export type CallApp = z.infer<typeof CallAppSchema>;

const RECORD_REFUSED_REASONS = [
  'not_active',
  'excluded',
  'paused',
  'busy',
  'permission_missing',
  'unavailable',
] as const;
export type RecordRefusedReason = (typeof RECORD_REFUSED_REASONS)[number];
type Refusal = Exclude<RecordRefusedReason, 'not_active'>;

const CallEvent = z.object({ call_id: CallIdSchema });
const HostEventSchema = z.discriminatedUnion('type', [
  CallEvent.extend({
    type: z.literal('call_detected'),
    app: CallAppSchema,
    site: z.string().nullable(),
    expires_at: z.string().datetime({ offset: true }),
  }),
  CallEvent.extend({
    type: z.literal('call_closed'),
    reason: z.enum(['call_ended', 'expired', 'paused', 'excluded']),
  }),
  CallEvent.extend({
    type: z.literal('recording'),
    app: CallAppSchema,
    started_at: z.string(),
  }),
  CallEvent.extend({
    type: z.literal('recording_stopped'),
    reason: z.enum([
      'user_stopped',
      'call_ended',
      'paused',
      'excluded',
      'permission_lost',
      'capture_failed',
      'transcriber_failed',
      'shutdown',
      'interrupted',
    ]),
  }),
  CallEvent.extend({
    type: z.literal('record_refused'),
    reason: z.enum(RECORD_REFUSED_REASONS),
  }),
]);
type HostEvent = z.infer<typeof HostEventSchema>;

type OfferedCall = { call_id: string; app: CallApp; site: string | null };
/**
 * The call the UI shows. `not_recording` is a call still in progress that the user declined
 * or let expire, or that the recorder refused: it can still be recorded from the tray.
 */
export type MeetingCall =
  | { kind: 'idle' }
  | ({ kind: 'awaiting_consent'; expires_at: string } & OfferedCall)
  | ({ kind: 'not_recording'; refused: Refusal | null } & OfferedCall)
  | ({ kind: 'recording'; started_at: string } & OfferedCall);

const IDLE: MeetingCall = { kind: 'idle' };

/** What the user or the consent deadline did to the call on screen. */
type LocalEvent = { type: 'declined' | 'consent_expired'; call_id: string };

/**
 * The next call state. The recorder holds one call at a time; an event about any other call
 * than the one shown is stale and changes nothing, except a detection or a start, which name
 * the call the recorder now holds.
 */
function nextMeetingCall(call: MeetingCall, event: HostEvent | LocalEvent): MeetingCall {
  const site = call.kind !== 'idle' && call.call_id === event.call_id ? call.site : null;
  if (event.type === 'call_detected') {
    const { type: _detected, ...detected } = event;
    return { kind: 'awaiting_consent', ...detected };
  }
  if (event.type === 'recording') {
    const { type: _recording, ...recording } = event;
    return { kind: 'recording', site, ...recording };
  }
  if (call.kind === 'idle' || call.call_id !== event.call_id) return call;
  const missed = (refused: Refusal | null) =>
    ({ kind: 'not_recording', call_id: call.call_id, app: call.app, site, refused }) as const;
  if (event.type === 'recording_stopped') return IDLE;
  if (call.kind === 'recording') return call;
  if (event.type === 'record_refused') {
    return event.reason === 'not_active' ? IDLE : missed(event.reason);
  }
  if (event.type === 'call_closed' && event.reason !== 'expired') return IDLE;
  // Declined, or the consent expired: the call stays recordable while it lasts.
  return call.kind === 'awaiting_consent' ? missed(null) : call;
}

/** One stdout line: a host event, or null for an empty line, an unknown event or bad JSON. */
function parseHostEvent(line: string): HostEvent | null {
  if (!line.trim()) return null;
  try {
    const parsed = HostEventSchema.safeParse(JSON.parse(line));
    if (parsed.success) return parsed.data;
    // An unknown type is a newer recorder's event; a known one that fails breaks the contract.
    console.error('Zanei host event ignored:', parsed.error.message);
  } catch (error) {
    console.error('Zanei host event is not JSON:', error);
  }
  return null;
}

/** Splits a UTF-8 stdout stream into lines; a chunk can end in the middle of one. */
function createLineSplitter(onLine: (line: string) => void): (chunk: string) => void {
  let rest = '';
  return (chunk) => {
    const lines = (rest + chunk).split('\n');
    rest = lines.pop() ?? '';
    for (const line of lines) onLine(line);
  };
}

export type MeetingCommand = { kind: 'record' | 'stop'; call_id: string } | { kind: 'prepare' };
/** `no_recorder`: no recorder runs to take the command, so nothing was queued. */
export type MeetingCommandResult = 'requested' | 'no_recorder';
/** What transcription needs from macOS; the helper asks for both in `prepare`. */
export type MeetingPermission = 'microphone' | 'audio_capture';
/** As the recorder reports it in `zanei status --json`. */
export type MeetingAvailability =
  | 'available'
  | { action_required: (MeetingPermission | 'speech_model')[] }
  | { unavailable: 'os_too_old' | 'no_apple_silicon' | 'locale_unsupported' | 'source_off' };
export type MeetingRecorder = {
  meeting: (command: MeetingCommand) => Promise<MeetingCommandResult>;
  readMeetingAvailability: () => Promise<MeetingAvailability | null>;
};

export function createMeetingTranscript(params: {
  getRecorder: () => MeetingRecorder | null;
  onChange: (call: MeetingCall) => void;
  /** The current owner's settings file; null while there is no owner. */
  settingsPath: () => string | null;
  getManifestPath: () => string;
  /** Whether this Mac can transcribe calls; read from macOS unless a test sets it. */
  supportsTranscription?: () => boolean;
}) {
  /** macOS 26 or later on Apple silicon, and a staged recorder release that can host it. */
  const isSupported = () =>
    (params.supportsTranscription?.() ??
      (process.arch === 'arm64' && Number(process.getSystemVersion().split('.')[0]) >= 26)) &&
    recorderTranscribesCalls(readZaneiManifest(params.getManifestPath()));
  let call: MeetingCall = IDLE;
  let consentTimer: NodeJS.Timeout | null = null;
  function apply(event: HostEvent | LocalEvent | null): void {
    const next = event ? nextMeetingCall(call, event) : IDLE;
    if (next === call) return;
    call = next;
    if (consentTimer) clearTimeout(consentTimer);
    consentTimer = null;
    if (call.kind === 'awaiting_consent') {
      // The recorder closes the consent at the same deadline; the prompt goes at the
      // deadline whether or not its event has arrived.
      const callId = call.call_id;
      consentTimer = setTimeout(
        () => apply({ type: 'consent_expired', call_id: callId }),
        Math.max(0, Date.parse(call.expires_at) - Date.now())
      );
    }
    params.onChange(call);
  }
  /** A settings file that cannot be read records no call: the feature reads as off. */
  const readSettings = (): MeetingSettings => {
    const filePath = params.settingsPath();
    if (!filePath || !fs.existsSync(filePath)) return DEFAULT_MEETING_SETTINGS;
    try {
      return MeetingSettingsSchema.parse(JSON.parse(fs.readFileSync(filePath, 'utf8')));
    } catch (error) {
      console.error('Meeting transcription settings are unreadable:', error);
      return DEFAULT_MEETING_SETTINGS;
    }
  };
  const send = (command: MeetingCommand) =>
    params.getRecorder()?.meeting(command) ?? Promise.resolve<MeetingCommandResult>('no_recorder');
  return {
    getCall: () => call,
    isRecording: () => call.kind === 'recording',
    /** Reads one recorder process's stdout; a line it left unfinished dies with it. */
    hostEventReader: () =>
      createLineSplitter((line) => {
        const event = parseHostEvent(line);
        if (event) apply(event);
      }),
    /** The recorder stopped: its calls and call ids ended with it. */
    reset: () => apply(null),
    decline: (callId: string) => apply({ type: 'declined', call_id: callId }),
    /** The outcome arrives as a host event: `recording` or `record_refused`. */
    record: (callId: string) => send({ kind: 'record', call_id: callId }),
    stop: (callId: string) => send({ kind: 'stop', call_id: callId }),
    /** Asks macOS for the microphone and system audio once, and fetches the speech model. */
    prepare: () => send({ kind: 'prepare' }),
    readAvailability: async () => (await params.getRecorder()?.readMeetingAvailability()) ?? null,
    getSettings: () => ({ settings: readSettings(), supported: isSupported() }),
    /** Null leaves transcription off in the recorder's configuration. */
    transcription: (): MeetingSettings | null => {
      const settings = readSettings();
      return settings.enabled && isSupported() ? settings : null;
    },
    /** Only turning transcription on or off changes `capture.sources`. */
    keepsSources: (next: MeetingSettings) => next.enabled === readSettings().enabled,
    writeSettings: (next: MeetingSettings) => {
      const filePath = params.settingsPath();
      if (!filePath) throw new Error('Local owner is unavailable.');
      fs.writeFileSync(`${filePath}.tmp`, JSON.stringify(next, null, 2), { mode: 0o600 });
      fs.renameSync(`${filePath}.tmp`, filePath);
    },
  };
}

const uniqueIgnoringCase = (values: string[]) =>
  new Set(values.map((value) => value.toLowerCase())).size === values.length;
/** The recorder refuses a whole configuration with one bad entry, so entries are checked here. */
const listOf = (entry: z.ZodString) =>
  z.array(entry).max(200).refine(uniqueIgnoringCase, 'Duplicate entry.').nullable();
const DOMAIN_LABEL = '[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?';

/**
 * The user's transcription settings. A null list leaves the recorder's default list in place.
 * Off by default: the recorder records no call unless the user turned this on once.
 */
export const MeetingSettingsSchema = z
  .object({
    enabled: z.boolean(),
    /** Bundle identifiers, as the recorder matches call apps. */
    callApps: listOf(z.string().regex(/^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$/)),
    /** Meeting site hosts; a host covers its subdomains. */
    callSites: listOf(
      z
        .string()
        .max(253)
        .regex(new RegExp(`^${DOMAIN_LABEL}(\\.${DOMAIN_LABEL})*\\.?$`))
    ),
    locale: z
      .string()
      .regex(/^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$/)
      .nullable(),
  })
  .strict();
export type MeetingSettings = z.infer<typeof MeetingSettingsSchema>;
const DEFAULT_MEETING_SETTINGS: MeetingSettings = {
  enabled: false,
  callApps: null,
  callSites: null,
  locale: null,
};
