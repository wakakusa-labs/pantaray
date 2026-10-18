import { createConnection, type Socket } from 'node:net';
import path from 'node:path';
import type {
  ConnectionConfiguration,
  ConnectionRoute,
  IntegrationCredential,
  LlmConnection,
  WebSearchCredential,
} from '../aiConnection/runtimeConnection';

const LOCAL_BACKEND_CONTROL_SOCKET_DIRNAME = 'local-backend';
const LOCAL_BACKEND_CONTROL_SOCKET_FILENAME = 'control.sock';
const CONTROL_SOCKET_REQUEST_TIMEOUT_MS = 5_000;
const CONFIGURE_OPERATION = 'configure';
const SET_CLOUD_SESSION_OPERATION = 'set_cloud_session';
const CLEAR_CLOUD_SESSION_OPERATION = 'clear_cloud_session';

type LoggerLike = {
  warn?: (message: string, payload?: unknown) => void;
  error?: (message: string, payload?: unknown) => void;
};

type JwtClaims = {
  exp?: unknown;
  sub?: unknown;
};

export type CloudSessionState = 'present' | 'expired' | 'absent';
export type ClearCloudSessionReason = 'signed_out' | 'expired';

/** main が保持する cloud session。helper の起動ごとに `configure` で丸ごと渡す。 */
export type CloudSessionSnapshot =
  | { state: 'present'; accountUserId: string; accessToken: string; sessionVersion: string }
  | { state: 'expired'; accountUserId: string; sessionVersion: string }
  | { state: 'absent' };

type PresentCloudSessionPayload = {
  state: 'present';
  account_user_id: string;
  access_token: string;
  expires_at: string;
  session_version: string;
};

type CloudSessionPayload =
  | PresentCloudSessionPayload
  | { state: 'expired'; account_user_id: string; session_version: string }
  | { state: 'absent' };

type ClearCloudSessionPayload = {
  reason: ClearCloudSessionReason;
  account_user_id: string;
  session_version: string;
  credential_generation: number;
  helper_instance_id: string;
};

type ControlSocketRequest =
  | {
      operation: typeof CONFIGURE_OPERATION;
      payload: {
        cloud_session: CloudSessionPayload;
        llm_connection: LlmConnection | null;
        web_search_credential: WebSearchCredential | null;
        integration_credential: IntegrationCredential | null;
      };
    }
  | { operation: 'set_llm_connection'; payload: LlmConnection }
  | { operation: 'set_web_search_credential'; payload: WebSearchCredential }
  | { operation: 'set_integration_credential'; payload: IntegrationCredential }
  | {
      operation:
        | 'clear_llm_connection'
        | 'clear_web_search_credential'
        | 'clear_integration_credential'
        | 'status';
      payload: Record<string, never>;
    }
  | { operation: typeof SET_CLOUD_SESSION_OPERATION; payload: PresentCloudSessionPayload }
  | { operation: typeof CLEAR_CLOUD_SESSION_OPERATION; payload: ClearCloudSessionPayload };

export type CloudSessionResult = {
  cloudSessionState: CloudSessionState;
  /** helper がトークンを受け取るたびに進む番号。以後の clear に添える。 */
  credentialGeneration: number;
  /** この generation を採番した helper。clear はこの組で fence される。 */
  helperInstanceId: string;
};

export type ClearCloudSessionResult = {
  cloudSessionState: CloudSessionState;
  /** 真なら helper は何もしていない（古い要求）。 */
  stale: boolean;
};

export type LocalConnectionStatus = {
  helperInstanceId: string;
  activeOwnerId: string;
  configured: boolean;
  cloudSessionState: CloudSessionState;
  llmRoute: ConnectionRoute;
  webSearchRoute: ConnectionRoute;
};

export type LocalBackendSessionSync = {
  configure: (
    cloudSession: CloudSessionSnapshot,
    connections: ConnectionConfiguration
  ) => Promise<CloudSessionResult>;
  setLlmConnection: (connection: LlmConnection | null) => Promise<ConnectionRoute>;
  setWebSearchCredential: (credential: WebSearchCredential | null) => Promise<ConnectionRoute>;
  setIntegrationCredential: (credential: IntegrationCredential | null) => Promise<void>;
  getConnectionStatus: () => Promise<LocalConnectionStatus>;
  setCloudSession: (
    accountUserId: string,
    accessToken: string,
    sessionVersion: string
  ) => Promise<CloudSessionResult>;
  clearCloudSession: (params: {
    reason: ClearCloudSessionReason;
    accountUserId: string;
    sessionVersion: string;
    credentialGeneration: number;
    helperInstanceId: string;
  }) => Promise<ClearCloudSessionResult>;
};

type SocketConnector = (socketPath: string) => Socket;

function requireNonEmptyString(value: unknown, fieldName: string): string {
  if (typeof value !== 'string') {
    throw new Error(`${fieldName} must be a string.`);
  }
  const normalized = value.trim();
  if (!normalized) {
    throw new Error(`${fieldName} must not be empty.`);
  }
  return normalized;
}

function requirePositiveIntegerString(value: unknown, fieldName: string): string {
  const normalized = requireNonEmptyString(value, fieldName);
  const parsed = Number.parseInt(normalized, 10);
  if (!Number.isInteger(parsed) || String(parsed) !== normalized || parsed <= 0) {
    throw new Error(`${fieldName} must be a positive integer string.`);
  }
  return normalized;
}

function decodeJwtClaims(token: string): JwtClaims {
  const parts = String(token).split('.');
  if (parts.length !== 3) {
    throw new Error('desktop_access_token must be a JWT.');
  }
  let payloadB64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
  const pad = (4 - (payloadB64.length % 4)) % 4;
  if (pad) {
    payloadB64 += '='.repeat(pad);
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(Buffer.from(payloadB64, 'base64').toString('utf8'));
  } catch {
    throw new Error('desktop_access_token payload is invalid JSON.');
  }
  if (!parsed || typeof parsed !== 'object') {
    throw new Error('desktop_access_token payload must be an object.');
  }
  return parsed as JwtClaims;
}

function buildPresentCloudSessionPayload(
  accountUserId: string,
  accessToken: string,
  sessionVersion: string
): PresentCloudSessionPayload {
  const normalizedUserId = requireNonEmptyString(accountUserId, 'account_user_id');
  const normalizedToken = requireNonEmptyString(accessToken, 'access_token');
  const claims = decodeJwtClaims(normalizedToken);
  const claimUserId = requireNonEmptyString(claims.sub, 'access_token.sub');
  if (claimUserId !== normalizedUserId) {
    throw new Error('access_token subject does not match account_user_id.');
  }
  if (!Number.isInteger(claims.exp) || Number(claims.exp) <= 0) {
    throw new Error('access_token.exp must be a positive integer.');
  }
  const expiresAt = new Date(Number(claims.exp) * 1000);
  if (Number.isNaN(expiresAt.getTime())) {
    throw new Error('access_token.exp is invalid.');
  }
  return {
    state: 'present',
    account_user_id: normalizedUserId,
    access_token: normalizedToken,
    expires_at: expiresAt.toISOString(),
    session_version: requirePositiveIntegerString(sessionVersion, 'session_version'),
  };
}

function buildCloudSessionPayload(snapshot: CloudSessionSnapshot): CloudSessionPayload {
  if (snapshot.state === 'present') {
    return buildPresentCloudSessionPayload(
      snapshot.accountUserId,
      snapshot.accessToken,
      snapshot.sessionVersion
    );
  }
  if (snapshot.state === 'expired') {
    return {
      state: 'expired',
      account_user_id: requireNonEmptyString(snapshot.accountUserId, 'account_user_id'),
      session_version: requirePositiveIntegerString(snapshot.sessionVersion, 'session_version'),
    };
  }
  return { state: 'absent' };
}

export function resolveLocalBackendControlSocketPath(userDataDir: string): string {
  const normalizedUserDataDir = requireNonEmptyString(userDataDir, 'userDataDir');
  return path.join(
    normalizedUserDataDir,
    LOCAL_BACKEND_CONTROL_SOCKET_DIRNAME,
    LOCAL_BACKEND_CONTROL_SOCKET_FILENAME
  );
}

function readCloudSessionState(value: unknown): CloudSessionState {
  const state = requireNonEmptyString(value, 'cloud_session_state');
  if (state === 'present' || state === 'expired' || state === 'absent') {
    return state;
  }
  throw new Error(`cloud_session_state is unknown: ${state}`);
}

function readRoute(value: unknown): ConnectionRoute {
  if (value === 'cloud' || value === 'direct' || value === 'unconfigured') return value;
  throw new Error('Local control socket returned an invalid connection route.');
}

function readCredentialGeneration(value: unknown): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) {
    throw new Error('credential_generation must be a non-negative integer.');
  }
  return value;
}

function parseControlSocketResponse(rawResponse: string): Record<string, unknown> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(rawResponse);
  } catch {
    throw new Error('Local control socket response must be valid JSON.');
  }
  if (!parsed || typeof parsed !== 'object') {
    throw new Error('Local control socket response must be an object.');
  }
  const response = parsed as Record<string, unknown>;
  if (typeof response.ok !== 'boolean') {
    throw new Error('Local control socket response must include ok.');
  }
  if (!response.ok) {
    const errorCode = requireNonEmptyString(response.error_code, 'error_code');
    const message = requireNonEmptyString(response.message, 'message');
    throw new Error(`Local control socket request failed (${errorCode}): ${message}`);
  }
  return response;
}

function sendControlSocketRequest(params: {
  socketPath: string;
  request: ControlSocketRequest;
  timeoutMs: number;
  connect?: SocketConnector;
}): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    const connect = params.connect ?? ((socketPath: string) => createConnection(socketPath));
    const socket = connect(params.socketPath);
    let settled = false;
    let responseBuffer = '';

    const rejectOnce = (error: Error): void => {
      if (settled) {
        return;
      }
      settled = true;
      socket.destroy();
      reject(error);
    };

    const resolveOnce = (response: Record<string, unknown>): void => {
      if (settled) {
        return;
      }
      settled = true;
      socket.end();
      resolve(response);
    };

    socket.on('connect', () => {
      socket.write(`${JSON.stringify(params.request)}\n`);
    });
    socket.setTimeout(params.timeoutMs);
    socket.on('timeout', () => {
      rejectOnce(new Error(`Local control socket request timed out after ${params.timeoutMs} ms.`));
    });
    socket.on('data', (chunk: Buffer | string) => {
      responseBuffer += Buffer.isBuffer(chunk) ? chunk.toString('utf8') : String(chunk);
      const newlineIndex = responseBuffer.indexOf('\n');
      if (newlineIndex < 0) {
        return;
      }
      try {
        const rawResponse = responseBuffer.slice(0, newlineIndex);
        resolveOnce(parseControlSocketResponse(rawResponse));
      } catch (error) {
        rejectOnce(error instanceof Error ? error : new Error(String(error)));
      }
    });
    socket.on('end', () => {
      if (!settled) {
        rejectOnce(new Error('Local control socket closed before sending a response.'));
      }
    });
    socket.on('error', (error: Error) => {
      rejectOnce(error);
    });
  });
}

export function createLocalBackendSessionSync(params: {
  getControlSocketPath: () => string;
  logger?: LoggerLike;
  connect?: SocketConnector;
  requestTimeoutMs?: number;
}): LocalBackendSessionSync {
  let pending = Promise.resolve();
  const requestTimeoutMs = params.requestTimeoutMs ?? CONTROL_SOCKET_REQUEST_TIMEOUT_MS;

  const enqueue = <T>(operation: () => Promise<T>): Promise<T> => {
    const result = pending.then(operation, operation);
    pending = result.then(
      () => undefined,
      () => undefined
    );
    return result.catch((error) => {
      params.logger?.error?.('LOCAL_BACKEND_SESSION_SYNC_ERR', { error });
      throw error;
    });
  };

  const send = (request: ControlSocketRequest): Promise<Record<string, unknown>> =>
    sendControlSocketRequest({
      socketPath: params.getControlSocketPath(),
      request,
      timeoutMs: requestTimeoutMs,
      connect: params.connect,
    });

  const readCloudSessionResult = (response: Record<string, unknown>): CloudSessionResult => ({
    cloudSessionState: readCloudSessionState(response.cloud_session_state),
    credentialGeneration: readCredentialGeneration(response.credential_generation),
    helperInstanceId: requireNonEmptyString(response.helper_instance_id, 'helper_instance_id'),
  });

  return {
    configure: (cloudSession, connections) =>
      enqueue(async () => {
        const payload = buildCloudSessionPayload(cloudSession);
        const response = await send({
          operation: CONFIGURE_OPERATION,
          payload: {
            cloud_session: payload,
            llm_connection: connections.llmConnection,
            web_search_credential: connections.webSearchCredential,
            integration_credential: connections.integrationCredential,
          },
        });
        if (response.configured !== true) {
          throw new Error('Local control socket configure response is invalid.');
        }
        return readCloudSessionResult(response);
      }),
    setLlmConnection: (connection) =>
      enqueue(async () => {
        const response = await send(
          connection
            ? { operation: 'set_llm_connection', payload: connection }
            : { operation: 'clear_llm_connection', payload: {} }
        );
        return readRoute(response.llm_route);
      }),
    setWebSearchCredential: (credential) =>
      enqueue(async () => {
        const response = await send(
          credential
            ? { operation: 'set_web_search_credential', payload: credential }
            : { operation: 'clear_web_search_credential', payload: {} }
        );
        return readRoute(response.web_search_route);
      }),
    setIntegrationCredential: (credential) =>
      enqueue(async () => {
        const response = await send(
          credential
            ? { operation: 'set_integration_credential', payload: credential }
            : { operation: 'clear_integration_credential', payload: {} }
        );
        if (response.integration_configured !== (credential !== null)) {
          throw new Error('Local control socket integration credential response is invalid.');
        }
      }),
    getConnectionStatus: () =>
      enqueue(async () => {
        const response = await send({ operation: 'status', payload: {} });
        if (typeof response.configured !== 'boolean') {
          throw new Error('Local control socket returned an invalid configured status.');
        }
        return {
          helperInstanceId: requireNonEmptyString(
            response.helper_instance_id,
            'helper_instance_id'
          ),
          activeOwnerId: requireNonEmptyString(response.active_owner_id, 'active_owner_id'),
          configured: response.configured,
          cloudSessionState: readCloudSessionState(response.cloud_session_state),
          llmRoute: readRoute(response.llm_route),
          webSearchRoute: readRoute(response.web_search_route),
        };
      }),
    setCloudSession: (accountUserId, accessToken, sessionVersion) =>
      enqueue(async () => {
        const response = await send({
          operation: SET_CLOUD_SESSION_OPERATION,
          payload: buildPresentCloudSessionPayload(accountUserId, accessToken, sessionVersion),
        });
        return readCloudSessionResult(response);
      }),
    clearCloudSession: ({
      reason,
      accountUserId,
      sessionVersion,
      credentialGeneration,
      helperInstanceId,
    }) =>
      enqueue(async () => {
        const response = await send({
          operation: CLEAR_CLOUD_SESSION_OPERATION,
          payload: {
            reason,
            account_user_id: requireNonEmptyString(accountUserId, 'account_user_id'),
            session_version: requirePositiveIntegerString(sessionVersion, 'session_version'),
            credential_generation: readCredentialGeneration(credentialGeneration),
            helper_instance_id: requireNonEmptyString(helperInstanceId, 'helper_instance_id'),
          },
        });
        if (typeof response.stale !== 'boolean') {
          throw new Error('Local control socket clear response is invalid.');
        }
        return {
          cloudSessionState: readCloudSessionState(response.cloud_session_state),
          stale: response.stale,
        };
      }),
  };
}
