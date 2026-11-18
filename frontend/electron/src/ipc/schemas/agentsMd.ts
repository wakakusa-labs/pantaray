import { z } from 'zod';

/**
 * The Action reads at most this much of `~/.pantaray/AGENTS.md` and cuts the rest
 * (AGENTS_MD_MAX_BYTES in agents/src/pantaray_agents/agents/action_agent/runtime/agents_md.py),
 * so a longer file is refused here rather than saved and silently cut.
 */
export const AGENTS_MD_MAX_BYTES = 32 * 1024;

export function utf8ByteLength(text: string): number {
  return new TextEncoder().encode(text).length;
}

/**
 * What was on disk when the text was read: the SHA-256 (hex) of the file's bytes, or `null`
 * when there was no file. A hash, not mtime, so a same-second atomic replace still differs.
 */
export const AgentsMdRevisionSchema = z
  .string()
  .regex(/^[0-9a-f]{64}$/)
  .nullable();
export type AgentsMdRevision = z.infer<typeof AgentsMdRevisionSchema>;

/** A file over the limit is not offered for editing: a cut copy would be saved over it. */
export type AgentsMdReadResult =
  | { kind: 'loaded'; content: string; revision: AgentsMdRevision }
  | { kind: 'too_large' };

export const AgentsMdWriteRequestSchema = z
  .object({
    content: z.string().refine((content) => utf8ByteLength(content) <= AGENTS_MD_MAX_BYTES),
    /** The revision the text was edited from; the write is refused if the file has moved on. */
    expectedRevision: AgentsMdRevisionSchema,
  })
  .strict();
export type AgentsMdWriteRequest = z.infer<typeof AgentsMdWriteRequestSchema>;

/** `conflict`: the file changed, appeared or was removed since it was read; nothing was written. */
export type AgentsMdWriteResult =
  | { kind: 'saved'; revision: NonNullable<AgentsMdRevision> }
  | { kind: 'conflict' };
