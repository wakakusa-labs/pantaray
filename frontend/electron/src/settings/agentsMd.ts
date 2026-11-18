import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

import { writeFileAtomic } from '../atomicFile';
import {
  AGENTS_MD_MAX_BYTES,
  type AgentsMdReadResult,
  type AgentsMdRevision,
  type AgentsMdWriteRequest,
  type AgentsMdWriteResult,
} from '../ipc/schemas/agentsMd';

export type AgentsMdFile = {
  read: () => AgentsMdReadResult;
  /** An empty text leaves the Action without instructions, as a blank file does. */
  write: (request: AgentsMdWriteRequest) => AgentsMdWriteResult;
};

type OnDisk = { kind: 'bytes'; bytes: Buffer | null } | { kind: 'too_large' };

const revisionOf = (bytes: Buffer): string => createHash('sha256').update(bytes).digest('hex');

/**
 * The Pantaray-wide instructions every Action reads when it starts
 * (agents/src/pantaray_agents/agents/action_agent/runtime/agents_md.py). The file is the only
 * copy: Settings reads and writes it directly.
 */
export function createAgentsMdFile(homeDir: string): AgentsMdFile {
  const filePath = path.join(homeDir, '.pantaray', 'AGENTS.md');

  /** At most one byte past the limit, so an oversized file is never read whole into main. */
  const readOnDisk = (): OnDisk => {
    let descriptor: number;
    try {
      // O_NONBLOCK so a FIFO planted under the name cannot block main, as in the Action's reader.
      descriptor = fs.openSync(filePath, fs.constants.O_RDONLY | fs.constants.O_NONBLOCK);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return { kind: 'bytes', bytes: null };
      throw error;
    }
    try {
      // The Action reads only a regular file; anything else gives it no instructions.
      if (!fs.fstatSync(descriptor).isFile()) return { kind: 'bytes', bytes: null };
      const buffer = Buffer.alloc(AGENTS_MD_MAX_BYTES + 1);
      let length = 0;
      while (length < buffer.length) {
        const count = fs.readSync(descriptor, buffer, length, buffer.length - length, null);
        if (count === 0) break;
        length += count;
      }
      if (length > AGENTS_MD_MAX_BYTES) return { kind: 'too_large' };
      return { kind: 'bytes', bytes: buffer.subarray(0, length) };
    } finally {
      fs.closeSync(descriptor);
    }
  };

  const revisionOnDisk = (onDisk: OnDisk & { kind: 'bytes' }): AgentsMdRevision =>
    onDisk.bytes === null ? null : revisionOf(onDisk.bytes);

  return {
    read: () => {
      const onDisk = readOnDisk();
      if (onDisk.kind === 'too_large') return onDisk;
      return {
        kind: 'loaded',
        // Invalid UTF-8 becomes U+FFFD, as the Action decodes it.
        content: onDisk.bytes?.toString('utf8') ?? '',
        revision: revisionOnDisk(onDisk),
      };
    },
    write: ({ content, expectedRevision }) => {
      const onDisk = readOnDisk();
      // Design limit: another writer landing between this check and the rename is not
      // detected; that window is one synchronous call in main.
      if (onDisk.kind === 'too_large' || revisionOnDisk(onDisk) !== expectedRevision) {
        return { kind: 'conflict' };
      }
      const bytes = Buffer.from(content, 'utf8');
      writeFileAtomic(filePath, path.dirname(filePath), bytes);
      return { kind: 'saved', revision: revisionOf(bytes) };
    },
  };
}
