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

export const AgentsMdContentSchema = z
  .string()
  .refine((content) => utf8ByteLength(content) <= AGENTS_MD_MAX_BYTES);
