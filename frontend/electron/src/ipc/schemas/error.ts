/**
 * IPC payload validation error + parse helper.
 *
 * Renderer-supplied payloads are validated at the IPC entry point. On
 * failure we throw `IpcValidationError`. The error's `message` is generic
 * (`"Invalid payload for <channel>"`) so internal field paths are not
 * exposed across the process boundary; the raw Zod issues stay main-side
 * for logging and test assertions.
 */

import type { ZodIssue, ZodType, ZodTypeDef } from 'zod';

export class IpcValidationError extends Error {
  readonly channel: string;
  readonly issues: ZodIssue[];

  constructor(channel: string, issues: ZodIssue[]) {
    super(`Invalid payload for ${channel}`);
    this.name = 'IpcValidationError';
    this.channel = channel;
    this.issues = issues;
  }
}

// The parsed shape may differ from the payload shape when a schema transforms a field.
export function parseInput<T>(
  schema: ZodType<T, ZodTypeDef, unknown>,
  channel: string,
  payload: unknown
): T {
  const result = schema.safeParse(payload);
  if (result.success) {
    return result.data;
  }
  throw new IpcValidationError(channel, result.error.issues);
}
