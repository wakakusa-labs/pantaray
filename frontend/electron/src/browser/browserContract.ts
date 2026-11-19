/**
 * The browser wire contract: the backend's operation and close requests
 * (`schema/websocket/server_messages.py`) and Electron's answer to `POST /local/action-browser`
 * (`local_runtime/runtime/browser_contract.py`). Both sides validate
 * `tests/fixtures/browser_contract_cases.json`. Only `page_data` carries page text.
 */

import { z } from 'zod';

import { ActionWireContractError } from '../actions/actionContracts';

/** The longest `wait` an operation may ask for; the `wait` budget derives from it. */
export const BROWSER_MAX_WAIT_SECONDS = 10;
/** Electron refuses a new host past this many per session. */
export const BROWSER_MAX_CONTACTED_HOSTS = 50;
export const BROWSER_MAX_BLOCKED_REQUESTS = 10;

/** Every object on this wire refuses fields it does not name. */
const strict = <T extends z.ZodRawShape>(shape: T) => z.object(shape).strict();

const NonBlankSchema = z.string().regex(/\S/);
const RefSchema = z.string().regex(/^\S+$/);
// A host name or an IP literal, IPv6 without brackets, as the command network reports one.
const HOST = /^(?:[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.?|[0-9A-Fa-f.]*:[0-9A-Fa-f:.]*)$/;
const HostSchema = z.string().regex(HOST);
const Sha256Schema = z.string().regex(/^[0-9a-f]{64}$/);
const PositiveIntSchema = z.number().int().positive();

const NavigateOperationSchema = strict({
  action: z.literal('navigate'),
  target: z.discriminatedUnion('kind', [
    // No blank, backslash or control character: the backend's URL parser would read
    // another host than Chromium's. Same rule as the backend's BrowserWebTarget.
    // eslint-disable-next-line no-control-regex
    strict({ kind: z.literal('web'), url: z.string().regex(/^https?:\/\/[^\s\\\x00-\x1f\x7f]+$/) }),
    // A local document `read` may open, by its real path.
    strict({ kind: z.literal('file'), path: z.string().regex(/^\//) }),
  ]),
});

const WaitOperationSchema = strict({
  action: z.literal('wait'),
  text: NonBlankSchema.nullish(),
  seconds: z.number().positive().max(BROWSER_MAX_WAIT_SECONDS).nullish(),
}).refine((wait) => (wait.text == null) !== (wait.seconds == null), {
  message: 'wait takes exactly one of text and seconds',
});

const ScreenshotOperationSchema = strict({
  action: z.literal('screenshot'),
  ref: RefSchema.nullish(),
  full_page: z.literal(true).nullish(),
}).refine((shot) => shot.ref == null || shot.full_page == null, {
  message: 'screenshot takes at most one of ref and full_page',
});

const BrowserOperationSchema = z.union([
  NavigateOperationSchema,
  strict({ action: z.literal('back') }),
  strict({ action: z.literal('snapshot'), ref: RefSchema.nullish() }),
  strict({ action: z.literal('click'), ref: RefSchema }),
  strict({ action: z.literal('hover'), ref: RefSchema }),
  strict({
    action: z.literal('type'),
    ref: RefSchema,
    text: z.string(),
    submit: z.boolean().optional(),
  }),
  strict({ action: z.literal('press'), key: NonBlankSchema }),
  strict({ action: z.literal('select'), ref: RefSchema, values: z.array(z.string()).min(1) }),
  strict({
    action: z.literal('scroll'),
    ref: RefSchema.nullish(),
    direction: z.enum(['up', 'down', 'left', 'right']),
  }),
  WaitOperationSchema,
  ScreenshotOperationSchema,
  // Print the page to a PDF, the answer's artifact.
  strict({ action: z.literal('print') }),
  strict({ action: z.literal('console') }),
]);

/** What the session's page may load; the session applies the latest policy it received. */
const BrowserPagePolicySchema = strict({
  allow_network: z.boolean(),
  file_roots: z.array(z.string()),
  private_roots: z.array(z.string()),
  private_root_exceptions: z.array(z.string()),
  secret_paths: z.array(strict({ withheld: z.string(), excepted: z.string().nullable() })),
  // External hosts this run may send writes to.
  write_hosts: z.array(HostSchema),
});

const BrowserOperationRequestSchema = strict({
  operation_request_id: NonBlankSchema,
  session_id: NonBlankSchema,
  operation: BrowserOperationSchema,
  policy: BrowserPagePolicySchema,
});

const BrowserSessionCloseRequestSchema = strict({ session_id: NonBlankSchema });

const ContactedHostsSchema = z
  .array(
    strict({
      host: HostSchema,
      port: z.number().int().min(1).max(65_535),
    })
  )
  .max(BROWSER_MAX_CONTACTED_HOSTS);

const BrowserPageDataSchema = strict({
  url: z.string(),
  title: z.string(),
  // The accessibility snapshot, for `snapshot` only.
  snapshot: z.string().nullable(),
  // The console lines, for `console` only.
  console: z
    .array(
      strict({
        level: z.enum(['debug', 'log', 'info', 'warning', 'error']),
        text: z.string(),
      })
    )
    .nullable(),
  popups: z.array(z.string()),
  blocked: z
    .array(
      strict({
        reason: z.enum([
          'outside_read_scope',
          'network_off',
          'write_not_approved',
          'not_allowed',
          'too_many_hosts',
        ]),
        url: z.string(),
      })
    )
    .max(BROWSER_MAX_BLOCKED_REQUESTS),
});

const BrowserArtifactClaimSchema = z.discriminatedUnion('kind', [
  strict({
    kind: z.literal('png'),
    byte_size: PositiveIntSchema,
    sha256: Sha256Schema,
    width_px: PositiveIntSchema,
    height_px: PositiveIntSchema,
  }),
  strict({ kind: z.literal('pdf'), byte_size: PositiveIntSchema, sha256: Sha256Schema }),
]);

/** The body of `POST /local/action-browser`. */
export const BrowserOperationAnswerSchema = strict({
  operation_request_id: NonBlankSchema,
  result: z.discriminatedUnion('status', [
    strict({
      status: z.literal('ok'),
      page_data: BrowserPageDataSchema,
      notes: z.array(z.enum(['dialog_dismissed', 'popup_refused', 'download_refused'])),
      truncated: z.enum(['snapshot_too_long', 'page_too_tall', 'console_too_long']).nullable(),
      // Hosts reached since the session's previous answer.
      network: ContactedHostsSchema,
      // A PNG for `screenshot`, a PDF for `print`; null for every other operation.
      artifact: BrowserArtifactClaimSchema.nullable(),
    }),
    strict({
      status: z.literal('failed'),
      code: z.enum([
        'BROWSER_NO_PAGE',
        'BROWSER_REF_UNKNOWN',
        'BROWSER_REF_STALE',
        'BROWSER_NAVIGATION_FAILED',
        'BROWSER_TIMED_OUT',
        'BROWSER_RENDERER_CRASHED',
        'BROWSER_SESSION_GONE',
        'BROWSER_ACTION_FAILED',
      ]),
      message: z.string(),
      network: ContactedHostsSchema,
    }),
  ]),
});

export type BrowserOperation = z.infer<typeof BrowserOperationSchema>;
export type BrowserPagePolicy = z.infer<typeof BrowserPagePolicySchema>;
export type BrowserOperationRequest = z.infer<typeof BrowserOperationRequestSchema>;
export type BrowserSessionCloseRequest = z.infer<typeof BrowserSessionCloseRequestSchema>;
export type BrowserPageData = z.infer<typeof BrowserPageDataSchema>;
export type BrowserArtifactClaim = z.infer<typeof BrowserArtifactClaimSchema>;
export type BrowserOperationAnswer = z.infer<typeof BrowserOperationAnswerSchema>;

export function parseBrowserOperationRequest(payload: unknown): BrowserOperationRequest {
  const result = BrowserOperationRequestSchema.safeParse(payload);
  if (!result.success) throw new ActionWireContractError('browser operation request');
  return result.data;
}

export function parseBrowserSessionCloseRequest(payload: unknown): BrowserSessionCloseRequest {
  const result = BrowserSessionCloseRequestSchema.safeParse(payload);
  if (!result.success) throw new ActionWireContractError('browser session close request');
  return result.data;
}
