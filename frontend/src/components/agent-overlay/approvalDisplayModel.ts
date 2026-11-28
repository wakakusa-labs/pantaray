import type { MessageKey, UiLanguage } from '@/i18n/types';
import { formatBytes } from '@/lib/formatBytes';
import {
  describeTrigger,
  formatInstant,
  type ScheduleTrigger,
} from '@/components/schedules/scheduleText';
import type { ActionApprovalBlocker } from '../../../electron/src/actions/actionLiveCore';

export type ApprovalDetail = {
  labelKey: MessageKey;
  value: string;
};

// One top-level argument of an integration write. Short single-line text reads as a
// row; longer text keeps its line breaks in a scrolling block; nested values stay JSON.
export type ApprovalArgumentField = {
  label: string;
  format: 'inline' | 'text' | 'json';
  value: string;
};

export type ApprovalDecision = 'approved_once' | 'approved_for_conversation' | 'denied';

export type ApprovalOutsideFolder = {
  path: string;
  displayName: string;
};

export type ApprovalOutsideWorkspace = {
  // Never empty: an approval without folders is not an outside-workspace one.
  folders: ApprovalOutsideFolder[];
  canAllowForConversation: boolean;
  hintKey: MessageKey;
};

export type ApprovalDisplay = {
  operationKey: MessageKey;
  operationVars?: Record<string, string>;
  // A model-written, user-facing sentence; when present it replaces the operation
  // line and the tool's own description moves behind a disclosure.
  reason: string | null;
  // The operation line already says it; a reason headline replaces that line, so the
  // panel repeats this on its own.
  usesLoginEnvironment: boolean;
  // The command keeps running after the call, until the work ends or it is stopped.
  runsInBackground: boolean;
  // The command runs with the user's own permissions, so the panel states that
  // risk in Pantaray's words and offers only a one-time approval.
  runsOutsideSandbox: boolean;
  primaryLabelKey: MessageKey;
  primaryValue: string;
  details: ApprovalDetail[];
  argumentFields: ApprovalArgumentField[];
  outsideWorkspace: ApprovalOutsideWorkspace | null;
  // A decision without a label is not offered for this approval.
  decisionLabelKeys: Partial<Record<ApprovalDecision, MessageKey>>;
};

type ToolApprovalDisplay = Pick<
  ApprovalDisplay,
  | 'operationKey'
  | 'operationVars'
  | 'usesLoginEnvironment'
  | 'primaryLabelKey'
  | 'primaryValue'
  | 'details'
  | 'argumentFields'
>;

// Longer text would stretch a label/value row; it gets its own block instead.
const INLINE_ARGUMENT_MAX_CHARS = 80;

export type ApprovalDisplayTranslator = (
  key: MessageKey,
  vars?: Record<string, string | number>
) => string;

function readStringValue(record: Record<string, unknown>, key: string): string | null {
  const value = record[key];
  return typeof value === 'string' && value.trim() ? value : null;
}

function readNumberValue(record: Record<string, unknown>, key: string): number | null {
  const value = record[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function readStringArrayValue(record: Record<string, unknown>, key: string): string[] {
  const value = record[key];
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === 'string' && item.trim().length > 0);
}

function buildGenericPrimaryValue(summary: Record<string, unknown>): string {
  return Object.entries(summary)
    .filter(([, value]) => value !== null && value !== undefined)
    .map(([key, value]) => `${key}: ${typeof value === 'string' ? value : JSON.stringify(value)}`)
    .join('\n');
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isNested(value: unknown): value is object {
  return typeof value === 'object' && value !== null;
}

function formatPlainValue(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value);
}

function toArgumentField(key: string, value: unknown): ApprovalArgumentField {
  const label = key.replace(/_/g, ' ');
  // A list of plain values, such as cc addresses, reads like a single value.
  const plainList =
    Array.isArray(value) && value.length > 0 && value.every((item) => !isNested(item));
  if (isNested(value) && !plainList) {
    return { label, format: 'json', value: JSON.stringify(value, null, 2) };
  }
  const text = plainList ? value.map(formatPlainValue).join(', ') : formatPlainValue(value);
  return text.length > INLINE_ARGUMENT_MAX_CHARS || text.includes('\n')
    ? { label, format: 'text', value: text }
    : { label, format: 'inline', value: text };
}

function parseJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch (error) {
    if (error instanceof SyntaxError) return text;
    throw error;
  }
}

// The runtime binds the approval to the arguments as a JSON string. Anything that is
// not an object is still shown whole, since the user approves exactly what will be sent.
// The backend serializes Python values: its ints are unbounded, while its floats are
// shortest-repr doubles that parse back exactly. An integer beyond 2^53 would display
// rounded, so the user would approve a different ID or amount than the one that runs.
function hasUnsafeInteger(value: unknown): boolean {
  if (typeof value === 'number') return Number.isInteger(value) && !Number.isSafeInteger(value);
  return isNested(value) && Object.values(value).some(hasUnsafeInteger);
}

function readIntegrationArguments(
  summary: Record<string, unknown>,
  t: ApprovalDisplayTranslator
): ApprovalArgumentField[] {
  const raw = summary.arguments;
  const parsed = typeof raw === 'string' ? parseJson(raw) : raw;
  if (isRecord(parsed) && !hasUnsafeInteger(parsed)) {
    return Object.entries(parsed).map(([key, value]) => toArgumentField(key, value));
  }
  return raw === undefined
    ? []
    : [
        {
          label: t('overlay.approvalRequired.arguments'),
          format: 'json',
          value: typeof raw === 'string' ? raw : JSON.stringify(raw, null, 2),
        },
      ];
}

function readOutsideFolder(record: unknown): ApprovalOutsideFolder | null {
  if (!isRecord(record)) return null;
  const path = readStringValue(record, 'path');
  const displayName = readStringValue(record, 'display_name');
  return path && displayName ? { path, displayName } : null;
}

// Any tool that would act outside the registered workspace carries this key, so
// the panel keys on it rather than on the tool.
function readOutsideWorkspace(summary: Record<string, unknown>): ApprovalOutsideWorkspace | null {
  const record = summary.outside_workspace;
  if (!isRecord(record)) return null;
  const folders = Array.isArray(record.folders)
    ? record.folders
        .map(readOutsideFolder)
        .filter((folder): folder is ApprovalOutsideFolder => folder !== null)
    : [];
  return folders.length > 0
    ? {
        folders,
        canAllowForConversation: record.can_allow_for_conversation === true,
        hintKey:
          folders.length === 1
            ? 'overlay.approvalRequired.outsideWorkspace.hint'
            : 'overlay.approvalRequired.outsideWorkspace.hintMultipleFolders',
      }
    : null;
}

// The display name of the user's own browser a browser approval acts in; null for the
// app's own page and for every other approval.
function readUsersBrowser(summary: Record<string, unknown>): string | null {
  const summaryKind = readStringValue(summary, 'summary_kind');
  return summaryKind === 'user_browser_read' || summaryKind === 'browser_write'
    ? readStringValue(summary, 'browser')
    : null;
}

// The tool's trigger argument, read as the schedule the user will see saved.
function readTrigger(trigger: Record<string, unknown>): ScheduleTrigger {
  if (trigger.kind === 'on')
    return {
      kind: 'on',
      on: trigger.on === 'microphone' ? 'microphone' : 'activity',
      match: (Array.isArray(trigger.match) ? trigger.match.filter(isRecord) : []).map((match) => ({
        field: readStringValue(match, 'field') ?? '',
        op: match.op === 'contains' ? 'contains' : 'equals',
        values: readStringArrayValue(match, 'values'),
      })),
    };
  const at = readStringValue(trigger, 'at');
  return at
    ? { kind: 'at', at }
    : {
        kind: 'every',
        every: readStringValue(trigger, 'every') ?? '',
        offset: readStringValue(trigger, 'offset'),
      };
}

function describeHook(
  summary: Record<string, unknown>,
  t: ApprovalDisplayTranslator,
  language: UiLanguage
): ToolApprovalDisplay {
  const trigger = readTrigger(isRecord(summary.trigger) ? summary.trigger : {});
  const until = readStringValue(summary, 'until');
  const place = isRecord(summary.place) ? summary.place : {};
  const none = t('overlay.approvalRequired.hook.none');
  const isNewSession = summary.send_to === 'new_session';
  const newSession: ApprovalDetail[] = isNewSession
    ? [
        {
          labelKey: 'overlay.approvalRequired.hook.title',
          value: readStringValue(summary, 'title') ?? t('overlay.approvalRequired.unavailable'),
        },
        {
          labelKey: 'overlay.approvalRequired.hook.organization',
          value: readStringValue(place, 'organization') ?? none,
        },
        {
          labelKey: 'overlay.approvalRequired.hook.project',
          value: readStringValue(place, 'project') ?? none,
        },
      ]
    : [];
  return {
    // A yes on a change to a hook that is off or stopped turns it back on.
    operationKey:
      summary.replaces !== true
        ? 'overlay.approvalRequired.operation.hook'
        : summary.resumes === true
          ? 'overlay.approvalRequired.operation.hookResume'
          : 'overlay.approvalRequired.operation.hookUpdate',
    usesLoginEnvironment: false,
    primaryLabelKey: 'overlay.approvalRequired.hook.when',
    primaryValue: describeTrigger(trigger, t, language),
    details: [
      ...(until
        ? [
            {
              labelKey: 'overlay.approvalRequired.hook.until' as MessageKey,
              value: formatInstant(until, language),
            },
          ]
        : []),
      {
        labelKey: 'overlay.approvalRequired.hook.sendTo',
        value: t(
          isNewSession
            ? 'overlay.approvalRequired.hook.newSession'
            : 'overlay.approvalRequired.hook.thisSession'
        ),
      },
      ...newSession,
      // The event's fields go to the model with the message, so the user learns that
      // more than the message leaves.
      ...(trigger.kind === 'on'
        ? [
            {
              labelKey: 'overlay.approvalRequired.hook.alsoSent' as MessageKey,
              value: t(
                trigger.on === 'microphone'
                  ? 'overlay.approvalRequired.hook.matchedMicrophone'
                  : 'overlay.approvalRequired.hook.matchedWindow'
              ),
            },
          ]
        : []),
    ],
    argumentFields: [
      {
        label: t('overlay.approvalRequired.hook.message'),
        format: 'text',
        value: readStringValue(summary, 'message') ?? t('overlay.approvalRequired.unavailable'),
      },
    ],
  };
}

export function buildApprovalDisplay(
  approvalPanel: ActionApprovalBlocker,
  t: ApprovalDisplayTranslator,
  language: UiLanguage
): ApprovalDisplay {
  const toolDisplay = buildToolApprovalDisplay(approvalPanel, t, language);
  // In the user's own browser the run acts signed in as the user. Pantaray's line
  // saying so must stay in view, so the model's reason does not replace it and is
  // shown as a row instead.
  const reason =
    readUsersBrowser(approvalPanel.commandSummary) === null
      ? readStringValue(approvalPanel.commandSummary, 'reason')
      : null;
  const runsInBackground = approvalPanel.commandSummary.run_in_background === true;
  if (approvalPanel.commandSummary.run_outside_sandbox === true) {
    return {
      ...toolDisplay,
      reason,
      runsInBackground,
      runsOutsideSandbox: true,
      outsideWorkspace: null,
      decisionLabelKeys: {
        approved_once: 'overlay.approvalRequired.outsideWorkspace.approveOnce',
        denied: 'overlay.approvalRequired.outsideWorkspace.deny',
      },
    };
  }
  const outsideWorkspace = readOutsideWorkspace(approvalPanel.commandSummary);
  if (!outsideWorkspace) {
    return {
      ...toolDisplay,
      reason,
      runsInBackground,
      runsOutsideSandbox: false,
      outsideWorkspace: null,
      decisionLabelKeys: {
        approved_once: 'overlay.approvalRequired.approveOnce',
        denied: 'overlay.approvalRequired.deny',
      },
    };
  }
  // The question names a single folder; with several, the folder list names them
  // under the tool's own description.
  const question: Pick<ApprovalDisplay, 'operationKey' | 'operationVars'> =
    outsideWorkspace.folders.length === 1
      ? {
          operationKey: 'overlay.approvalRequired.outsideWorkspace.operation',
          operationVars: { folder: outsideWorkspace.folders[0].displayName },
        }
      : { operationKey: toolDisplay.operationKey };
  return {
    ...toolDisplay,
    ...question,
    reason,
    runsInBackground,
    runsOutsideSandbox: false,
    outsideWorkspace,
    decisionLabelKeys: {
      approved_once: 'overlay.approvalRequired.outsideWorkspace.approveOnce',
      denied: 'overlay.approvalRequired.outsideWorkspace.deny',
      ...(outsideWorkspace.canAllowForConversation
        ? {
            approved_for_conversation:
              'overlay.approvalRequired.outsideWorkspace.approveForConversation',
          }
        : {}),
    },
  };
}

function buildToolApprovalDisplay(
  approvalPanel: ActionApprovalBlocker,
  t: ApprovalDisplayTranslator,
  language: UiLanguage
): ToolApprovalDisplay {
  const summary = approvalPanel.commandSummary;
  const summaryKind = readStringValue(summary, 'summary_kind');
  const cwd = readStringValue(summary, 'cwd');

  if (approvalPanel.toolId === 'bash' || summaryKind === 'bash') {
    const usesLoginEnvironment = summary.use_login_environment === true;
    return {
      operationKey: usesLoginEnvironment
        ? 'overlay.approvalRequired.operation.bashLoginEnvironment'
        : 'overlay.approvalRequired.operation.bash',
      usesLoginEnvironment,
      primaryLabelKey: 'overlay.approvalRequired.command',
      primaryValue:
        readStringValue(summary, 'command') ?? t('overlay.approvalRequired.unavailable'),
      details: [
        ...(cwd ? [{ labelKey: 'overlay.approvalRequired.cwd' as MessageKey, value: cwd }] : []),
      ],
      argumentFields: [],
    };
  }

  if (approvalPanel.toolId === 'run_python' || summaryKind === 'run_python') {
    const codeSizeBytes = readNumberValue(summary, 'code_size_bytes');
    const argsCount = readNumberValue(summary, 'args_count');
    return {
      operationKey: 'overlay.approvalRequired.operation.runPython',
      usesLoginEnvironment: false,
      primaryLabelKey: 'overlay.approvalRequired.pythonCode',
      primaryValue: t('overlay.approvalRequired.pythonCodeDescription'),
      details: [
        ...(cwd ? [{ labelKey: 'overlay.approvalRequired.cwd' as MessageKey, value: cwd }] : []),
        ...(codeSizeBytes !== null
          ? [
              {
                labelKey: 'overlay.approvalRequired.size' as MessageKey,
                value: formatBytes(codeSizeBytes),
              },
            ]
          : []),
        ...(argsCount !== null
          ? [
              {
                labelKey: 'overlay.approvalRequired.arguments' as MessageKey,
                value: String(argsCount),
              },
            ]
          : []),
      ],
      argumentFields: [],
    };
  }

  if (approvalPanel.toolId === 'apply_patch' || summaryKind === 'apply_patch') {
    const targetPaths = readStringArrayValue(summary, 'target_paths');
    return {
      operationKey: 'overlay.approvalRequired.operation.applyPatch',
      usesLoginEnvironment: false,
      primaryLabelKey: 'overlay.approvalRequired.files',
      primaryValue: targetPaths.length
        ? targetPaths.join('\n')
        : t('overlay.approvalRequired.unavailable'),
      details: [],
      argumentFields: [],
    };
  }

  // The slug, IDs and the service's own tool description bind the approval to this
  // exact write; the user approves the service, account, action and arguments.
  if (summaryKind === 'integration_tool') {
    const unavailable = t('overlay.approvalRequired.unavailable');
    return {
      operationKey: 'overlay.approvalRequired.operation.integration',
      operationVars: {
        service: readStringValue(summary, 'toolkit') ?? unavailable,
        account: readStringValue(summary, 'account') ?? unavailable,
      },
      usesLoginEnvironment: false,
      primaryLabelKey: 'overlay.approvalRequired.details',
      primaryValue: '',
      details: [
        {
          labelKey: 'overlay.approvalRequired.integration.tool',
          value: readStringValue(summary, 'tool') ?? unavailable,
        },
      ],
      argumentFields: readIntegrationArguments(summary, t),
    };
  }

  const usersBrowser = readUsersBrowser(summary);
  // A read opens the host in the run's tab of the user's browser; a write lets that
  // tab send data to the host. Both act as the user from their signed-in accounts.
  if (usersBrowser !== null) {
    const isRead = summaryKind === 'user_browser_read';
    const pageHost = readStringValue(summary, 'page_host');
    const why = readStringValue(summary, 'reason');
    return {
      operationKey: isRead
        ? 'overlay.approvalRequired.operation.userBrowserRead'
        : 'overlay.approvalRequired.operation.userBrowserWrite',
      operationVars: { browser: usersBrowser },
      usesLoginEnvironment: false,
      primaryLabelKey: isRead
        ? 'overlay.approvalRequired.site'
        : 'overlay.approvalRequired.sendsTo',
      primaryValue: readStringValue(summary, 'host') ?? t('overlay.approvalRequired.unavailable'),
      details: [
        ...(pageHost
          ? [{ labelKey: 'overlay.approvalRequired.openPage' as MessageKey, value: pageHost }]
          : []),
        ...(why ? [{ labelKey: 'overlay.approvalRequired.why' as MessageKey, value: why }] : []),
      ],
      argumentFields: [],
    };
  }

  // The host is what the run's browser pages may then send data to; the page host is
  // the site the run opened, so the user sees where the request comes from.
  if (summaryKind === 'browser_write') {
    const pageHost = readStringValue(summary, 'page_host');
    return {
      operationKey: 'overlay.approvalRequired.operation.browserWrite',
      usesLoginEnvironment: false,
      primaryLabelKey: 'overlay.approvalRequired.sendsTo',
      primaryValue: readStringValue(summary, 'host') ?? t('overlay.approvalRequired.unavailable'),
      details: pageHost
        ? [{ labelKey: 'overlay.approvalRequired.openPage' as MessageKey, value: pageHost }]
        : [],
      argumentFields: [],
    };
  }

  // The named app is exactly what Electron is asked to capture, so it is what the user
  // approves; the generic line would hide the act behind "this operation".
  if (approvalPanel.toolId === 'capture_screen' || summaryKind === 'screen_capture') {
    return {
      operationKey: 'overlay.approvalRequired.operation.captureScreen',
      usesLoginEnvironment: false,
      primaryLabelKey: 'overlay.approvalRequired.app',
      primaryValue:
        readStringValue(summary, 'app_name') ?? t('overlay.approvalRequired.unavailable'),
      details: [],
      argumentFields: [],
    };
  }

  // A hook sends messages later with no one watching, so the user approves when it
  // fires, where the message goes, and the exact message it sends.
  if (summaryKind === 'hook') return describeHook(summary, t, language);

  return {
    operationKey: 'overlay.approvalRequired.operation.generic',
    usesLoginEnvironment: false,
    primaryLabelKey: 'overlay.approvalRequired.details',
    primaryValue: buildGenericPrimaryValue(summary),
    details: [],
    argumentFields: [],
  };
}
