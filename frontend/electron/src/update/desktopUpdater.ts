/**
 * Desktop updater（macOS / electron-builder github provider）。
 *
 * 目的:
 * - 公開 GitHub Releases（electron-builder の `publish` 設定が `app-update.yml` に書く owner / repo）から更新を取得する
 * - 認証なしで動く。Pantaray へのログイン状態に依存しない
 * - 「自動アップデート」: 起動時と 6 時間ごとにチェック/ダウンロード
 * - 「手動アップデート」: Tray メニューから「更新を確認」でチェック
 *
 * 方針:
 * - 配布ビルドのバージョンは release workflow のタグと同じ `X.Y.Z`（stable）か
 *   `X.Y.Z-test.N`（test チャネル。prerelease を受け入れる）。それ以外（ローカルで packaged した
 *   `-dev` ビルドなど）は feed を持たず、更新チェックをしない
 * - 適用はデフォルトで「次回終了時に自動」+ 任意で「今すぐ再起動して更新」
 * - electron-updater の `update-downloaded` は zip を取り終えた時点で、Squirrel.Mac はその後に
 *   zip を受け取り、展開と署名の検証をする。インストールできるのは Electron の autoUpdater
 *   （Squirrel.Mac）が `update-downloaded` を出してからなので、それまでは `downloading` のまま
 */

import { app, autoUpdater as squirrelUpdater } from 'electron';
import { autoUpdater } from 'electron-updater';

type LoggerLike = {
  info?: (name: string, payload?: unknown) => void;
  warn?: (name: string, payload?: unknown) => void;
  error?: (name: string, payload?: unknown) => void;
};

/** 更新状態（手動チェック時のフィードバック用）。 */
export type UpdateState = 'idle' | 'downloading' | 'downloaded';

export type UpdateChannel = 'stable' | 'test';

export type DesktopUpdater = {
  /** パッケージ済みビルドで自動チェックを開始する（初回は即時、以後 6 時間ごと）。 */
  start: () => void;
  checkForUpdates: (reason: 'auto' | 'manual') => Promise<void>;
  isUpdateDownloaded: () => boolean;
  /** 現在の更新ダウンロード状態を返す。 */
  getUpdateState: () => UpdateState;
  /** ダウンロード中または完了済みの更新先バージョン文字列（例: "0.0.46"）。未検出なら null。 */
  getPendingVersion: () => string | null;
  quitAndInstall: () => void;
  dispose: () => void;
};

const QUIT_INSTALL_FALLBACK_TIMEOUT_MS = 6000;
// 過剰なリクエストを避けつつ、十分に追従する間隔。
const AUTO_CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;

// release-desktop.yml が受け付けるバージョンと同じ形式。prerelease 識別子は `test` だけ。
// electron-updater の GitHub provider は、現在のバージョンと同じ prerelease 識別子を持つ
// prerelease しか選ばないため、test チャネルの識別子は 1 つに固定する。
const RELEASE_VERSION = /^\d+\.\d+\.\d+(-test\.\d+)?$/;

/** 配布ビルドの更新チャネル。配布物ではないバージョン（`-dev` など）は null（更新しない）。 */
export function resolveChannelFromVersion(ver: string): UpdateChannel | null {
  const match = RELEASE_VERSION.exec(ver);
  if (!match) return null;
  return match[1] ? 'test' : 'stable';
}

/** packaged かつ配布ビルドのときだけ更新 feed がある。 */
export function hasUpdateFeed(): boolean {
  return app.isPackaged && resolveChannelFromVersion(app.getVersion()) !== null;
}

export function createDesktopUpdater(params: {
  logger: LoggerLike | null;
  onUpdateDownloaded?: () => void;
  onUpdateAvailable?: (payload: { reason: 'auto' | 'manual'; info: unknown }) => void;
  onUpdateNotAvailable?: (payload: { reason: 'auto' | 'manual'; info: unknown }) => void;
  onUpdateError?: (payload: { reason: 'auto' | 'manual'; error: unknown }) => void;
  beforeQuitAndInstall?: () => void;
}): DesktopUpdater {
  let disposed = false;
  let started = false;
  let updateDownloaded = false;
  let updateDownloading = false;
  let pendingVersion: string | null = null;
  let checking = false;
  let interval: NodeJS.Timeout | null = null;
  let lastCheckReason: 'auto' | 'manual' | null = null;

  function log(level: 'info' | 'warn' | 'error', name: string, payload?: unknown): void {
    try {
      params.logger?.[level]?.(name, payload);
    } catch {
      // no-op
    }
  }

  function notifyUpdateError(reason: 'auto' | 'manual', error: unknown): void {
    try {
      params.onUpdateError?.({ reason, error });
    } catch {
      // no-op
    }
  }

  function ensureEventHandlersInstalled(): void {
    // 多重登録を避けるため、dispose までに1回だけ登録する設計
    autoUpdater.on('checking-for-update', () => log('info', 'AUTO_UPDATE_CHECKING'));
    autoUpdater.on('update-available', (info) => {
      updateDownloading = true;
      pendingVersion =
        info && typeof (info as { version?: unknown }).version === 'string'
          ? (info as { version: string }).version
          : null;
      const reason = lastCheckReason;
      lastCheckReason = null;
      log('info', 'AUTO_UPDATE_AVAILABLE', { info, reason });
      if (reason) {
        try {
          params.onUpdateAvailable?.({ reason, info });
        } catch {
          // no-op
        }
      }
    });
    autoUpdater.on('update-not-available', (info) => {
      const reason = lastCheckReason;
      lastCheckReason = null;
      log('info', 'AUTO_UPDATE_NOT_AVAILABLE', { info, reason });
      if (reason) {
        try {
          params.onUpdateNotAvailable?.({ reason, info });
        } catch {
          // no-op
        }
      }
    });
    // MacUpdater は Squirrel.Mac の error もこのイベントに流す。Squirrel の準備前に失敗したら
    // idle に戻し、次の自動または手動のチェックでやり直せるようにする。
    autoUpdater.on('error', (err) => {
      updateDownloading = false;
      const reason = lastCheckReason;
      lastCheckReason = null;
      log('error', 'AUTO_UPDATE_ERR', { err, reason });
      if (reason) {
        try {
          params.onUpdateError?.({ reason, error: err });
        } catch {
          // no-op
        }
      }
    });
    autoUpdater.on('download-progress', (p) => log('info', 'AUTO_UPDATE_PROGRESS', { p }));
    autoUpdater.on('update-downloaded', (info) => {
      // zip を取り終えただけ。Squirrel.Mac の準備が終わるまで downloading のまま。
      lastCheckReason = null;
      log('info', 'AUTO_UPDATE_DOWNLOADED', { info });
    });
    squirrelUpdater.on('update-downloaded', () => {
      updateDownloaded = true;
      updateDownloading = false;
      log('info', 'AUTO_UPDATE_READY', { pendingVersion });
      try {
        params.onUpdateDownloaded?.();
      } catch {
        // no-op
      }
    });
  }

  // 初期設定。feed は app-update.yml（electron-builder の publish 設定）が決める。
  const channel = resolveChannelFromVersion(app.getVersion());
  const updatesEnabled = hasUpdateFeed();
  if (app.isPackaged && !updatesEnabled) {
    log('info', 'AUTO_UPDATE_DISABLED', { version: app.getVersion() });
  }
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.allowPrerelease = channel === 'test';
  autoUpdater.allowDowngrade = false;
  ensureEventHandlersInstalled();

  async function checkForUpdates(reason: 'auto' | 'manual'): Promise<void> {
    if (disposed || !updatesEnabled) return;
    // A later check would replace pendingVersion while Squirrel.Mac still prepares or holds
    // the earlier update, and the notice would then name a version the restart does not install.
    if (reason === 'auto' && (updateDownloading || updateDownloaded)) {
      log('info', 'AUTO_UPDATE_CHECK_SKIPPED', { pendingVersion });
      return;
    }
    if (checking) {
      // 起動直後の auto check 中に手動で「更新を確認」を押しても無反応に見えないよう、
      // 実行中のチェックの結果を手動チェック扱いに昇格させる。
      if (reason === 'manual') {
        lastCheckReason = 'manual';
      }
      return;
    }
    checking = true;
    try {
      lastCheckReason = reason;
      log('info', 'AUTO_UPDATE_CHECK_START', { reason, channel });
      await autoUpdater.checkForUpdates();
    } catch (e) {
      const r = lastCheckReason;
      lastCheckReason = null;
      log('error', 'AUTO_UPDATE_CHECK_EXCEPTION', { reason, err: e });
      if (r) {
        notifyUpdateError(r, e);
      }
    } finally {
      checking = false;
    }
  }

  return {
    start: () => {
      if (disposed || started || !updatesEnabled) return;
      started = true;
      interval = setInterval(() => {
        void checkForUpdates('auto');
      }, AUTO_CHECK_INTERVAL_MS);
      void checkForUpdates('auto');
    },
    checkForUpdates,
    isUpdateDownloaded: () => Boolean(updateDownloaded),
    getUpdateState: (): UpdateState => {
      if (updateDownloaded) return 'downloaded';
      if (updateDownloading) return 'downloading';
      return 'idle';
    },
    getPendingVersion: () => pendingVersion,
    quitAndInstall: () => {
      // Squirrel.Mac の準備前に終了すると何もインストールされない（下の強制終了も含めて）。
      if (!updateDownloaded) {
        log('warn', 'AUTO_UPDATE_QUIT_INSTALL_NOT_READY', { pendingVersion });
        return;
      }
      log('info', 'AUTO_UPDATE_QUIT_INSTALL_REQUESTED', { pendingVersion });
      try {
        params.beforeQuitAndInstall?.();
      } catch {
        // no-op
      }
      try {
        autoUpdater.quitAndInstall(false, true);
      } catch (e) {
        log('error', 'AUTO_UPDATE_QUIT_INSTALL_ERR', { err: e });
      }
      // macOS では window の close ハンドラが app.quit() をブロックし
      // プロセスが残り続ける場合がある（Dock に白い点が残る）。
      // ShipIt はプロセス終了を検知して更新を適用するため、
      // 更新時の通常終了は beforeQuitAndInstall で進める。
      // fallback は helper shutdown の 5 秒上限を超えてから強制終了する。
      setTimeout(() => {
        try {
          log('warn', 'AUTO_UPDATE_QUIT_FALLBACK', {
            message: 'app.quit() did not terminate within timeout; forcing exit',
          });
          app.exit(0);
        } catch {
          // no-op
        }
      }, QUIT_INSTALL_FALLBACK_TIMEOUT_MS);
    },
    dispose: () => {
      disposed = true;
      if (interval) clearInterval(interval);
      interval = null;
    },
  };
}
