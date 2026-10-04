import type { MessageKey } from '@/i18n/types';
import { formatBytes } from '@/lib/formatBytes';
import type { ActionApprovalBlocker } from '../../../electron/src/actions/actionLiveCore';

export type ApprovalDetail = {
  labelKey: MessageKey;
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
  // The command runs with the user's own permissions, so the panel states that
  // risk in Pantaray's words and offers only a one-time approval.
  runsOutsideSandbox: boolean;
  primaryLabelKey: MessageKey;
  primaryValue: string;
  details: ApprovalDetail[];
  outsideWorkspace: ApprovalOutsideWorkspace | null;
  // A decision without a label is not offered for this approval.
  decisionLabelKeys: Partial<Record<ApprovalDecision, MessageKey>>;
};

type ToolApprovalDisplay = Pick<
  ApprovalDisplay,
  'operationKey' | 'usesLoginEnvironment' | 'primaryLabelKey' | 'primaryValue' | 'details'
>;

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

function readOutsideFolder(value: unknown): ApprovalOutsideFolder | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const path = readStringValue(record, 'path');
  const displayName = readStringValue(record, 'display_name');
  return path && displayName ? { path, displayName } : null;
}

// Any tool that would act outside the registered workspace carries this key, so
// the panel keys on it rather than on the tool.
function readOutsideWorkspace(summary: Record<string, unknown>): ApprovalOutsideWorkspace | null {
  const value = summary.outside_workspace;
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
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

export function buildApprovalDisplay(
  approvalPanel: ActionApprovalBlocker,
  t: ApprovalDisplayTranslator
): ApprovalDisplay {
  const toolDisplay = buildToolApprovalDisplay(approvalPanel, t);
  const reason = readStringValue(approvalPanel.commandSummary, 'reason');
  if (approvalPanel.commandSummary.run_outside_sandbox === true) {
    return {
      ...toolDisplay,
      reason,
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
  t: ApprovalDisplayTranslator
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
    };
  }

  return {
    operationKey: 'overlay.approvalRequired.operation.generic',
    usesLoginEnvironment: false,
    primaryLabelKey: 'overlay.approvalRequired.details',
    primaryValue: buildGenericPrimaryValue(summary),
    details: [],
  };
}
