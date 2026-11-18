import fs from 'node:fs';
import path from 'node:path';

import { writeFileAtomic } from '../atomicFile';

export type AgentsMdFile = {
  /** The file's text, or `""` when there is none. */
  read: () => string;
  /** Replaces the file; an empty text leaves the Action without instructions, as a blank file does. */
  write: (content: string) => void;
};

/**
 * The Pantaray-wide instructions every Action reads when it starts
 * (agents/src/pantaray_agents/agents/action_agent/runtime/agents_md.py). The file is the only
 * copy: Settings reads and writes it directly.
 */
export function createAgentsMdFile(homeDir: string): AgentsMdFile {
  const filePath = path.join(homeDir, '.pantaray', 'AGENTS.md');
  return {
    read: () => {
      let descriptor: number;
      try {
        // O_NONBLOCK so a FIFO planted under the name cannot block main, as in the Action's reader.
        descriptor = fs.openSync(filePath, fs.constants.O_RDONLY | fs.constants.O_NONBLOCK);
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === 'ENOENT') return '';
        throw error;
      }
      try {
        // The Action reads only a regular file; anything else gives it no instructions.
        if (!fs.fstatSync(descriptor).isFile()) return '';
        // Invalid UTF-8 becomes U+FFFD, as the Action decodes it.
        return fs.readFileSync(descriptor).toString('utf8');
      } finally {
        fs.closeSync(descriptor);
      }
    },
    write: (content) =>
      writeFileAtomic(filePath, path.dirname(filePath), Buffer.from(content, 'utf8')),
  };
}
