import type { ConnectionConfiguration } from '../aiConnection/runtimeConnection';
import type { ExpiredCloudIdentity } from './contracts';
import type {
  ClearCloudSessionReason,
  CloudSessionResult,
  CloudSessionSnapshot,
  LocalBackendSessionSync,
} from './localBackendSessionSync';

type LoggerLike = {
  warn?: (message: string, payload?: unknown) => void;
  error?: (message: string, payload?: unknown) => void;
};

type LocalBackendHelperManagerLike = {
  ensureStarted: () => Promise<{ helperInstanceId: string }>;
  terminateCurrentHelper: () => Promise<void>;
};

export type AuthContextChangePayload = {
  token: string | null;
  userId: string | null;
  sessionVersion: string | null;
  /** 有効なセッションがあったが更新できていないアカウント（明示ログアウトとは別）。 */
  expiredIdentity: ExpiredCloudIdentity | null;
};

export type LocalBackendContainmentResult =
  | {
      contained: true;
    }
  | {
      contained: false;
      reason: string;
    };

/**
 * helper が持っている資格情報についてこのプロセスが知っていること。
 *
 * `unknown` は「要求が helper に届いたかどうか分からない」状態で、helper を
 * 終了させるまで解消しない。この状態を捨てるとサインアウトが「消すものがない」
 * と誤判定するため、封じ込めが成功するまで保持する。
 */
type HelperSession =
  | { kind: 'clean' }
  | {
      kind: 'tracked';
      accountUserId: string;
      sessionVersion: string;
      credentialGeneration: number;
      helperInstanceId: string;
    }
  | { kind: 'unknown' };

function normalizeOptionalString(value: string | null | undefined): string | null {
  if (typeof value !== 'string') {
    return null;
  }
  const normalized = value.trim();
  return normalized ? normalized : null;
}

function snapshotOf(payload: AuthContextChangePayload): CloudSessionSnapshot {
  const token = normalizeOptionalString(payload.token);
  const userId = normalizeOptionalString(payload.userId);
  const sessionVersion = normalizeOptionalString(payload.sessionVersion);
  if (token && userId && sessionVersion) {
    return { state: 'present', accountUserId: userId, accessToken: token, sessionVersion };
  }
  if ((token && userId && !sessionVersion) || (!token && sessionVersion)) {
    throw new Error('sessionVersion must be provided exactly when token and userId are present.');
  }
  if (payload.expiredIdentity) {
    return {
      state: 'expired',
      accountUserId: payload.expiredIdentity.userId,
      sessionVersion: payload.expiredIdentity.sessionVersion,
    };
  }
  return { state: 'absent' };
}

export function createLocalBackendAuthContextController(params: {
  sessionSync: LocalBackendSessionSync;
  helperManager: LocalBackendHelperManagerLike;
  getConnections: () => Promise<ConnectionConfiguration>;
  onRuntimeUnavailable: () => void;
  logger?: LoggerLike;
}): {
  applyAuthContextChange: (payload: AuthContextChangePayload) => Promise<void>;
  updateConnections: (change: () => void) => Promise<void>;
  /** 認証状態を変えず、ローカルで実行中の処理を helper ごと停止する。 */
  containRuntime: () => Promise<LocalBackendContainmentResult>;
  /** helper が cloud session を持たない状態にする。サインアウト前の封じ込め。 */
  clearForSignOut: () => Promise<LocalBackendContainmentResult>;
} {
  // 起動した helper の世代ごとに configure を 1 回送る。以後は差分だけを送る。
  let configuredHelperInstanceId: string | null = null;
  let session: HelperSession = { kind: 'clean' };
  let currentSnapshot: CloudSessionSnapshot | null = null;
  // 認証状態の変化と IPC のサインアウトは別経路から来る。tracked の読み取りと
  // 要求の送出が割り込まれないよう、この境界で直列化する。
  let queue: Promise<unknown> = Promise.resolve();

  const enqueue = <T>(operation: () => Promise<T>): Promise<T> => {
    const result = queue.then(operation, operation);
    queue = result.then(
      () => undefined,
      () => undefined
    );
    return result;
  };

  // helper ごと捨てる。成功したときだけ「資格情報は残っていない」と言える。
  async function discardHelper(error: unknown): Promise<LocalBackendContainmentResult> {
    params.logger?.warn?.('LOCAL_BACKEND_CLEAR_SESSION_FALLBACK', { error });
    try {
      await params.helperManager.terminateCurrentHelper();
      session = { kind: 'clean' };
      configuredHelperInstanceId = null;
      return { contained: true };
    } catch (terminateError) {
      params.logger?.error?.('LOCAL_BACKEND_CLEAR_SESSION_CONTAINMENT_FAILED', {
        error,
        terminateError,
      });
      return {
        contained: false,
        reason: terminateError instanceof Error ? terminateError.message : String(terminateError),
      };
    } finally {
      // Auth-state application can be waiting on this queue. Report without awaiting its queue.
      params.onRuntimeUnavailable();
    }
  }

  async function clearTracked(
    reason: ClearCloudSessionReason
  ): Promise<LocalBackendContainmentResult> {
    if (session.kind === 'unknown') {
      return discardHelper(new Error('Helper session state is unknown.'));
    }
    if (session.kind === 'clean') {
      return { contained: true };
    }
    const target = session;
    let clearError: unknown = null;
    try {
      const result = await params.sessionSync.clearCloudSession({
        reason,
        accountUserId: target.accountUserId,
        sessionVersion: target.sessionVersion,
        credentialGeneration: target.credentialGeneration,
        helperInstanceId: target.helperInstanceId,
      });
      if (!result.stale) {
        if (reason === 'signed_out') {
          session = { kind: 'clean' };
        }
        return { contained: true };
      }
      // The helper holds a session this main process did not issue, so the
      // credential it still has cannot be cleared by identity.
      clearError = new Error('Local control socket rejected the clear as stale.');
    } catch (error) {
      clearError = error;
    }
    return discardHelper(clearError);
  }

  // A session request that fails after reaching the helper leaves a credential
  // this process can no longer name, so the helper is discarded instead of kept
  // under a tracking state that would let a later sign-out skip its clear.
  const sendSession = async (
    send: () => Promise<CloudSessionResult>,
    snapshot: CloudSessionSnapshot
  ): Promise<void> => {
    let result: CloudSessionResult;
    try {
      result = await send();
    } catch (sendError) {
      session = { kind: 'unknown' };
      await discardHelper(sendError);
      throw sendError;
    }
    configuredHelperInstanceId = result.helperInstanceId;
    session =
      snapshot.state === 'absent'
        ? { kind: 'clean' }
        : {
            kind: 'tracked',
            accountUserId: snapshot.accountUserId,
            sessionVersion: snapshot.sessionVersion,
            credentialGeneration: result.credentialGeneration,
            helperInstanceId: result.helperInstanceId,
          };
  };

  const applyAuthContextChange = async (payload: AuthContextChangePayload): Promise<void> => {
    const snapshot = snapshotOf(payload);
    currentSnapshot = snapshot;
    if (session.kind === 'unknown') {
      // 前回の要求が届いたか分からない helper を残したまま次を送らない。
      const containment = await discardHelper(new Error('Helper session state is unknown.'));
      if (!containment.contained) {
        throw new Error(`Failed to contain the local backend helper: ${containment.reason}`);
      }
    }
    const { helperInstanceId } = await params.helperManager.ensureStarted();
    if (helperInstanceId !== configuredHelperInstanceId) {
      // The response carries the helper that issued the generation, so a
      // restart between ensureStarted and configure cannot mispair them.
      const connections = await params.getConnections();
      await sendSession(() => params.sessionSync.configure(snapshot, connections), snapshot);
      return;
    }
    if (snapshot.state === 'present') {
      await sendSession(
        () =>
          params.sessionSync.setCloudSession(
            snapshot.accountUserId,
            snapshot.accessToken,
            snapshot.sessionVersion
          ),
        snapshot
      );
      return;
    }
    const containment = await clearTracked(snapshot.state === 'expired' ? 'expired' : 'signed_out');
    if (!containment.contained) {
      throw new Error(`Failed to clear local backend auth context: ${containment.reason}`);
    }
  };

  // 呼び出し側の user id では失効中（token も user も null）の所有者を表せない。
  // helper が何を持っているかはこの境界が知っているので、それを基準に消す。
  const clearForSignOut = async (): Promise<LocalBackendContainmentResult> => {
    const result = await clearTracked('signed_out');
    if (result.contained) currentSnapshot = { state: 'absent' };
    return result;
  };

  const updateConnections = async (change: () => void): Promise<void> => {
    change();
    // Before cloud auth restoration completes, only persist. The first configure reads it all.
    if (!currentSnapshot) return;
    if (session.kind === 'unknown') {
      const result = await discardHelper(new Error('Helper session state is unknown.'));
      if (!result.contained) throw new Error('Failed to contain the local backend helper.');
    }
    let helperInstanceId: string;
    let connections: ConnectionConfiguration;
    try {
      ({ helperInstanceId } = await params.helperManager.ensureStarted());
      connections = await params.getConnections();
    } catch (error) {
      session = { kind: 'unknown' };
      await discardHelper(error);
      throw error;
    }
    const snapshot = currentSnapshot;
    if (helperInstanceId !== configuredHelperInstanceId) {
      await sendSession(() => params.sessionSync.configure(snapshot, connections), snapshot);
      return;
    }
    try {
      await params.sessionSync.setLlmConnection(connections.llmConnection);
      await params.sessionSync.setWebSearchCredential(connections.webSearchCredential);
      await params.sessionSync.setIntegrationCredential(connections.integrationCredential);
    } catch (error) {
      // A lost response can leave an old credential active. Stop the helper before reporting failure.
      session = { kind: 'unknown' };
      await discardHelper(error);
      throw error;
    }
  };

  return {
    containRuntime: () =>
      enqueue(() => {
        // If stopping fails, no later auth/configuration update may reuse this helper.
        session = { kind: 'unknown' };
        return discardHelper(new Error('Local runtime containment requested.'));
      }),
    updateConnections: (change) => enqueue(() => updateConnections(change)),
    applyAuthContextChange: (payload) => enqueue(() => applyAuthContextChange(payload)),
    clearForSignOut: () => enqueue(() => clearForSignOut()),
  };
}
