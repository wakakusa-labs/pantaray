import type { LocalRuntimeState } from '../auth/localRuntimeState';

type LoopbackBackendUrlModule = {
  parseLoopbackBackendUrl: (value: unknown, fieldName?: string) => URL;
};

// CommonJS SSOT is also consumed by pre-build JavaScript configuration scripts.
const loopbackBackendUrlModule =
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  require('../../loopback_backend_url.js') as LoopbackBackendUrlModule;
const { parseLoopbackBackendUrl } = loopbackBackendUrlModule;

type BackendErrorPayload = {
  type?: unknown;
  detail?:
    | string
    | {
        error_code?: unknown;
        message?: unknown;
      };
};

type HttpMethod = 'DELETE' | 'GET' | 'POST' | 'PUT';

type QueryValue = string | number | boolean;

export type LocalBackendRequest = {
  path: string;
  method: HttpMethod;
  query?: Record<string, QueryValue | null | undefined>;
  body?: unknown;
  timeoutMs?: number;
};

type RuntimeStateAccessor = () => LocalRuntimeState;

const AUTHENTICATION_REQUIRED_ERROR_CODE = 'AUTHENTICATION_REQUIRED';
const AUTHENTICATION_REQUIRED_FALLBACK_MESSAGE = 'Authentication required. Please sign in again.';
const LOCAL_BACKEND_REQUEST_TIMEOUT_ERROR_CODE = 'LOCAL_BACKEND_REQUEST_TIMEOUT';

const ALLOWED_LOCAL_BACKEND_ROUTES: ReadonlyArray<{
  path: RegExp;
  methods: ReadonlySet<HttpMethod>;
}> = [
  { path: /^\/local\/action-screen-capture$/, methods: new Set(['POST']) },
  { path: /^\/v1\/agents\/users\/[^/]+\/context-source$/, methods: new Set(['GET']) },
  { path: /^\/v1\/agents\/users\/[^/]+\/context-source\/transitions$/, methods: new Set(['POST']) },
  { path: /^\/v1\/agents\/users\/[^/]+\/suggestions\/welcome$/, methods: new Set(['POST']) },
  { path: /^\/api\/agent\/history$/, methods: new Set(['GET']) },
  {
    path: /^\/api\/agent\/history\/items\/(conversation|suggestion)\/[^/]+$/,
    methods: new Set(['DELETE']),
  },
  {
    path: /^\/api\/agent\/history\/[^/]+\/overlay-bootstrap$/,
    methods: new Set(['GET']),
  },
  {
    path: /^\/v1\/agents\/users\/[^/]+\/actions\/[^/]+\/approvals$/,
    methods: new Set(['POST']),
  },
  {
    path: /^\/v1\/agents\/users\/[^/]+\/actions\/[^/]+\/approval-mode$/,
    methods: new Set(['GET', 'PUT']),
  },
  {
    path: /^\/v1\/agents\/users\/[^/]+\/actions\/messages$/,
    methods: new Set(['POST']),
  },
  {
    path: /^\/v1\/agents\/users\/[^/]+\/actions\/[^/]+\/state$/,
    methods: new Set(['GET']),
  },
  {
    path: /^\/v1\/agents\/users\/[^/]+\/actions\/[^/]+\/steps\/[^/]+\/output$/,
    methods: new Set(['GET']),
  },
  {
    path: /^\/v1\/agents\/users\/[^/]+\/workspace-settings$/,
    methods: new Set(['GET']),
  },
  {
    path: /^\/v1\/agents\/users\/[^/]+\/workspace-settings\/(read-access-scope|command-network)$/,
    methods: new Set(['GET', 'PUT']),
  },
  {
    path: /^\/v1\/agents\/users\/[^/]+\/workspace-settings\/projects\/order$/,
    methods: new Set(['PUT']),
  },
  {
    path: /^\/v1\/agents\/users\/[^/]+\/workspace-settings\/(organizations|projects|folders)$/,
    methods: new Set(['POST']),
  },
  {
    path: /^\/v1\/agents\/users\/[^/]+\/workspace-settings\/(organizations|projects|folders)\/[^/]+$/,
    methods: new Set(['DELETE']),
  },
  {
    path: /^\/v1\/agents\/users\/[^/]+\/workspace-settings\/(projects|folders)\/[^/]+\/links$/,
    methods: new Set(['PUT']),
  },
  {
    path: /^\/v1\/agents\/users\/[^/]+\/approval-preferences\/workspace-edit-and-command$/,
    methods: new Set(['GET', 'PUT']),
  },
];

export class LocalBackendRequestError extends Error {
  readonly status: number | null;
  readonly errorCode: string | null;

  constructor(message: string, status: number | null = null, errorCode: string | null = null) {
    super(message);
    this.name = 'LocalBackendRequestError';
    this.status = status;
    this.errorCode = errorCode;
  }
}

function asTimeoutRequestError(error: unknown, url: string): LocalBackendRequestError | null {
  if (!(error instanceof Error)) return null;
  if (error.name !== 'AbortError' && error.name !== 'TimeoutError') return null;
  return new LocalBackendRequestError(
    `Failed to reach local backend endpoint: ${url} (${error.message})`,
    null,
    LOCAL_BACKEND_REQUEST_TIMEOUT_ERROR_CODE
  );
}

function buildRequestUrl(
  runtimeBackendUrl: string | null,
  path: string,
  method: HttpMethod,
  query?: Record<string, QueryValue | null | undefined>
): string {
  let baseUrl: URL;
  try {
    baseUrl = parseLoopbackBackendUrl(runtimeBackendUrl, 'Local backend URL');
  } catch (error) {
    throw new LocalBackendRequestError(
      error instanceof Error ? error.message : 'Local backend URL is invalid.'
    );
  }
  const isAllowedRoute = ALLOWED_LOCAL_BACKEND_ROUTES.some(
    (route) => route.path.test(path) && route.methods.has(method)
  );
  if (!isAllowedRoute) {
    throw new LocalBackendRequestError('Local backend route is not allowed.');
  }
  baseUrl.pathname = path;
  baseUrl.search = '';
  baseUrl.hash = '';
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === null || value === undefined) {
      continue;
    }
    params.set(key, String(value));
  }
  baseUrl.search = params.toString();
  return baseUrl.toString();
}

function extractErrorDetail(
  payload: unknown,
  fallback: string
): {
  errorCode: string | null;
  message: string;
} {
  const backendPayload =
    payload && typeof payload === 'object' ? (payload as BackendErrorPayload) : null;
  const topLevelErrorCode = typeof backendPayload?.type === 'string' ? backendPayload.type : null;
  if (backendPayload && typeof backendPayload.detail === 'object' && backendPayload.detail) {
    const detail = backendPayload.detail as {
      error_code?: unknown;
      message?: unknown;
    };
    return {
      errorCode: typeof detail.error_code === 'string' ? detail.error_code : topLevelErrorCode,
      message: typeof detail.message === 'string' ? detail.message : fallback,
    };
  }
  if (typeof backendPayload?.detail === 'string') {
    return {
      errorCode: topLevelErrorCode,
      message: backendPayload.detail,
    };
  }
  return { errorCode: topLevelErrorCode, message: fallback };
}

/**
 * The local API token belongs to the running helper, so a request is only
 * addressable once the runtime is configured and that helper still holds it.
 */
function requireLocalApiToken(runtimeState: LocalRuntimeState, token: string | null): string {
  if (runtimeState.status === 'degraded') {
    throw new LocalBackendRequestError(runtimeState.message ?? 'Local runtime is unavailable.');
  }
  if (runtimeState.status !== 'ready' || !token) {
    throw new LocalBackendRequestError('Local runtime is initializing.');
  }
  return token;
}

export function createLocalBackendClient(params: {
  getRuntimeBackendUrl: () => string | null;
  getLocalApiToken: () => string | null;
  getRuntimeState: RuntimeStateAccessor;
}): {
  requestJson: <T>(request: LocalBackendRequest) => Promise<T>;
} {
  return {
    requestJson: async <T>(request: LocalBackendRequest): Promise<T> => {
      const localApiToken = requireLocalApiToken(
        params.getRuntimeState(),
        params.getLocalApiToken()
      );
      const url = buildRequestUrl(
        params.getRuntimeBackendUrl(),
        request.path,
        request.method,
        request.query
      );

      let response: Response;
      try {
        response = await fetch(url, {
          method: request.method,
          redirect: 'error',
          headers: {
            Authorization: `Bearer ${localApiToken}`,
            Accept: 'application/json',
            ...(request.body === undefined ? {} : { 'Content-Type': 'application/json' }),
          },
          ...(request.body === undefined ? {} : { body: JSON.stringify(request.body) }),
          ...(request.timeoutMs === undefined
            ? {}
            : { signal: AbortSignal.timeout(request.timeoutMs) }),
        });
      } catch (error) {
        const timeoutError = asTimeoutRequestError(error, url);
        if (timeoutError) throw timeoutError;
        const detail = error instanceof Error ? error.message : String(error);
        throw new LocalBackendRequestError(
          `Failed to reach local backend endpoint: ${url} (${detail})`
        );
      }

      if (!response.ok) {
        let errorPayload: unknown = null;
        try {
          errorPayload = (await response.json()) as unknown;
        } catch (error) {
          const timeoutError = asTimeoutRequestError(error, url);
          if (timeoutError) throw timeoutError;
          errorPayload = null;
        }
        if (response.status === 401) {
          const detail = extractErrorDetail(errorPayload, AUTHENTICATION_REQUIRED_FALLBACK_MESSAGE);
          throw new LocalBackendRequestError(
            detail.message,
            response.status,
            detail.errorCode ?? AUTHENTICATION_REQUIRED_ERROR_CODE
          );
        }
        const fallback = 'Local backend request failed.';
        const detail = extractErrorDetail(errorPayload, fallback);
        throw new LocalBackendRequestError(detail.message, response.status, detail.errorCode);
      }
      // 204 carries no body; its callers type the result as `void`.
      if (response.status === 204) return undefined as T;
      try {
        return (await response.json()) as T;
      } catch (error) {
        throw asTimeoutRequestError(error, url) ?? error;
      }
    },
  };
}
