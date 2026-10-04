import type { UiLanguage } from '../ipc/context';
import type { CaptureStatusSnapshot } from '../screenshot/captureStatus';

export type UpdateMenuCopy = {
  checkUpdates: string;
  restartToUpdate: string;
  updateUnavailableTitle: string;
  updateUnavailableBody: string;
  checkFailedTitle: string;
  checkFailedBody: string;
  updateReadyTitle: string;
  updateReadyBody: string;
  laterLabel: string;
  downloadingTitle: string;
  downloadingBody: string;
  noUpdateTitle: string;
  noUpdateBody: string;
};

export type TrayMenuCopy = {
  newConversation: string;
  openWindow: string;
  startScreenshots: string;
  stopScreenshots: string;
  quit: string;
  captureStatus: (status: CaptureStatusSnapshot | null) => TrayCaptureStatusCopy;
  tip: (hasUpdateReady: boolean, status: CaptureStatusSnapshot | null) => string;
};

type TrayCaptureStatusCopy = {
  label: string;
  detail: string | null;
};

export type StartupDialogCopy = {
  runtimeConfigTitle: string;
  runtimeConfigBody: (detail: string) => string;
  startupTitle: string;
  startupBody: (detail: string) => string;
};

export function getUpdateMenuCopy(lang: UiLanguage): UpdateMenuCopy {
  return lang === 'ja'
    ? {
        checkUpdates: '更新を確認',
        restartToUpdate: '再起動して更新',
        updateUnavailableTitle: '更新を確認できません',
        updateUnavailableBody: 'インストール版でのみ更新を確認できます。',
        checkFailedTitle: '更新を確認できません',
        checkFailedBody: 'しばらくしてからもう一度お試しください。',
        updateReadyTitle: 'アップデートの準備ができました',
        updateReadyBody: '再起動すると更新が適用されます。',
        laterLabel: '後で',
        downloadingTitle: 'ダウンロード中...',
        downloadingBody: '更新プログラムをダウンロードしています。しばらくお待ちください。',
        noUpdateTitle: '更新はありません',
        noUpdateBody: '現在は最新バージョンです。',
      }
    : {
        checkUpdates: 'Check for updates',
        restartToUpdate: 'Restart to update',
        updateUnavailableTitle: 'Cannot check for updates',
        updateUnavailableBody: 'Updates are only available in the installed app.',
        checkFailedTitle: 'Unable to check for updates',
        checkFailedBody: 'Please try again later.',
        updateReadyTitle: 'Update ready',
        updateReadyBody: 'Restart to apply the update.',
        laterLabel: 'Later',
        downloadingTitle: 'Downloading...',
        downloadingBody: 'An update is being downloaded. Please wait.',
        noUpdateTitle: 'No updates available',
        noUpdateBody: 'You are already on the latest version.',
      };
}

function getTrayCaptureStatusCopy(
  status: CaptureStatusSnapshot | null,
  lang: UiLanguage
): TrayCaptureStatusCopy {
  if (!status) {
    return lang === 'ja'
      ? { label: 'Pantaray: 確認中', detail: null }
      : { label: 'Pantaray: checking', detail: null };
  }
  const appName = status.activeWindow?.appName || (lang === 'ja' ? '不明なアプリ' : 'Unknown app');
  const host = status.browserUrl?.host || (lang === 'ja' ? '不明なURL' : 'Unknown URL');

  switch (status.kind) {
    case 'capturing':
      return lang === 'ja'
        ? { label: 'Pantaray: 操作を記録中', detail: null }
        : { label: 'Pantaray: recording activity', detail: null };
    case 'degraded':
      // reasonLabel names recorder components (ax, chrome, ...); keep it in logs only.
      return lang === 'ja'
        ? {
            label: 'Pantaray: 一部の操作を記録できません',
            detail: '一部のアプリの情報を取得できません。そのアプリを再度操作すると再試行します',
          }
        : {
            label: 'Pantaray: activity recording is incomplete',
            detail:
              'Some app information cannot be observed. It is retried when you use that app again',
          };
    case 'blocked_by_url':
      return lang === 'ja'
        ? { label: `対象外: ${appName}`, detail: `${host} はURLルールでブロックされています` }
        : { label: `Out of scope: ${appName}`, detail: `${host} is blocked by URL rules` };
    case 'blocked_by_app':
      return lang === 'ja'
        ? { label: `対象外: ${appName}`, detail: '許可アプリではありません' }
        : { label: `Out of scope: ${appName}`, detail: 'Not an allowed app' };
    case 'blocked_by_ide':
      return lang === 'ja'
        ? { label: `対象外: ${appName}`, detail: 'IDEファイルルールでブロックされています' }
        : { label: `Out of scope: ${appName}`, detail: 'Blocked by IDE file rules' };
    case 'editing_paused':
      return lang === 'ja'
        ? { label: 'Pantaray: ルール編集中', detail: '操作の記録は一時停止中です' }
        : { label: 'Pantaray: editing rules', detail: 'Activity recording is paused' };
    case 'permission_required':
      return lang === 'ja'
        ? {
            label: 'Pantaray: 操作の記録に許可が必要',
            detail:
              'システム設定でPantarayのアクセシビリティ・入力監視・必要なブラウザのオートメーションを許可してから開始してください',
          }
        : {
            label: 'Pantaray: recording permission required',
            detail:
              'Allow Pantaray Accessibility, Input Monitoring and browser Automation in System Settings, then start again',
          };
    case 'disconnected':
      return lang === 'ja'
        ? { label: 'Pantaray: 接続待ち', detail: null }
        : { label: 'Pantaray: waiting for connection', detail: null };
    case 'paused':
      return lang === 'ja'
        ? { label: 'Pantaray: 停止中', detail: null }
        : { label: 'Pantaray: paused', detail: null };
    case 'checking':
      return lang === 'ja'
        ? { label: 'Pantaray: 確認中', detail: null }
        : { label: 'Pantaray: checking', detail: null };
    case 'waiting':
      return lang === 'ja'
        ? { label: `待機中: ${appName}`, detail: null }
        : { label: `Waiting: ${appName}`, detail: null };
    case 'unavailable':
      if (status.reasonLabel === 'capture_preference_write_failed') {
        return lang === 'ja'
          ? {
              label: 'Pantaray: 停止設定を保存できません',
              detail: '次回起動時に操作の記録が再開する可能性があります',
            }
          : {
              label: 'Pantaray: could not save the stop setting',
              detail: 'Activity recording may resume on the next launch',
            };
      }
      // The menu offers Pause only while recording is enabled, so the advice must match.
      if (lang === 'ja') {
        return {
          label: 'Pantaray: 操作の記録を利用できません',
          detail: status.screenshotsEnabled
            ? '記録を一度一時停止してから、もう一度再開してください'
            : '記録をもう一度再開してください',
        };
      }
      return {
        label: 'Pantaray: activity recording unavailable',
        detail: status.screenshotsEnabled
          ? 'Pause recording once, then resume it'
          : 'Resume recording',
      };
    default: {
      const unreachable: never = status.kind;
      return { label: `Pantaray: ${String(unreachable)}`, detail: null };
    }
  }
}

export function getTrayMenuCopy(lang: UiLanguage): TrayMenuCopy {
  return lang === 'ja'
    ? {
        newConversation: '新しい会話',
        openWindow: 'ウィンドウを開く',
        startScreenshots: '操作の記録を再開',
        stopScreenshots: '操作の記録を一時停止',
        quit: '終了',
        captureStatus: (status) => getTrayCaptureStatusCopy(status, 'ja'),
        tip: (hasUpdateReady, status) => {
          const base = getTrayCaptureStatusCopy(status, 'ja').label;
          return hasUpdateReady ? `${base}（更新あり）` : base;
        },
      }
    : {
        newConversation: 'New conversation',
        openWindow: 'Open window',
        startScreenshots: 'Resume activity recording',
        stopScreenshots: 'Pause activity recording',
        quit: 'Quit',
        captureStatus: (status) => getTrayCaptureStatusCopy(status, 'en'),
        tip: (hasUpdateReady, status) => {
          const base = getTrayCaptureStatusCopy(status, 'en').label;
          return hasUpdateReady ? `${base} (update ready)` : base;
        },
      };
}

export function getStartupDialogCopy(lang: UiLanguage): StartupDialogCopy {
  return lang === 'ja'
    ? {
        runtimeConfigTitle: 'Pantaray 起動設定エラー',
        runtimeConfigBody: (detail) =>
          [
            '必要な設定ファイルが見つからない、または不正です。',
            '',
            '- electron/runtime_config.json が同梱されていることを確認してください。',
            '',
            `詳細: ${detail}`,
          ].join('\n'),
        startupTitle: '起動エラー',
        startupBody: (detail) => `アプリケーションの起動中にエラーが発生しました: ${detail}`,
      }
    : {
        runtimeConfigTitle: 'Pantaray startup configuration error',
        runtimeConfigBody: (detail) =>
          [
            'A required configuration file is missing or invalid.',
            '',
            '- Confirm that electron/runtime_config.json is bundled.',
            '',
            `Details: ${detail}`,
          ].join('\n'),
        startupTitle: 'Startup error',
        startupBody: (detail) => `An error occurred while starting the app: ${detail}`,
      };
}

/** macOS draws modifiers as ⌃⌥⇧⌘ in that order, e.g. `Option+Space` → `⌥Space`. */
const MAC_MODIFIER_GLYPHS: Readonly<Record<string, { order: number; glyph: string }>> = {
  Control: { order: 0, glyph: '⌃' },
  Ctrl: { order: 0, glyph: '⌃' },
  Option: { order: 1, glyph: '⌥' },
  Alt: { order: 1, glyph: '⌥' },
  Shift: { order: 2, glyph: '⇧' },
  Command: { order: 3, glyph: '⌘' },
  Cmd: { order: 3, glyph: '⌘' },
  Super: { order: 3, glyph: '⌘' },
  CommandOrControl: { order: 3, glyph: '⌘' },
  CmdOrCtrl: { order: 3, glyph: '⌘' },
};

export function formatMacAccelerator(accelerator: string): string {
  const tokens = accelerator.split('+');
  const key = tokens.pop() ?? '';
  const modifiers = tokens
    .map((token, index) => ({ index, known: MAC_MODIFIER_GLYPHS[token], token }))
    .sort(
      (left, right) =>
        (left.known?.order ?? 4) - (right.known?.order ?? 4) || left.index - right.index
    )
    .map(({ known, token }) => known?.glyph ?? token);
  return `${modifiers.join('')}${key}`;
}

/**
 * The message shown when recording starts for an owner with no data, before there is anything
 * to suggest.
 * It names the shortcut the user actually has, or only the button when none is set.
 */
export function getWelcomeSuggestionText(lang: UiLanguage, accelerator: string | null): string {
  const shortcut = accelerator ? formatMacAccelerator(accelerator) : null;
  if (lang === 'ja') {
    const how = shortcut ? ` ${shortcut} か新しい会話ボタン` : '新しい会話ボタン';
    return [
      'まずはあなたの仕事を理解するところから始めます。お役に立てそうなことが見つかったら、こちらから提案します。',
      `それまでも、任せたい仕事があればいつでも${how}で声をかけてください。`,
    ].join('\n\n');
  }
  const how = shortcut
    ? `press ${shortcut} or use the New conversation button`
    : 'use the New conversation button';
  return [
    "I'll start by getting to know your work. When I find something I can help with, I'll suggest it.",
    `Until then, whenever you have work to hand off, just ${how}.`,
  ].join('\n\n');
}
