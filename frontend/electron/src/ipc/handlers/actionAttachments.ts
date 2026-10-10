/**
 * Composer document attachments (`action:attachFile`, `action:discardAttachment`).
 *
 * Like image attachments, Electron main writes the bytes under `LOCAL_ARTIFACT_ROOT` itself.
 * The file is only staged here: when the message is submitted, the local backend links
 * `{attachmentId}{extension}` into the Action's workspace and deletes the staged copy. A chip
 * the user removes before sending is discarded explicitly.
 *
 * The staged path is built from the session's user id, a fresh UUID and an extension from the
 * allowed list, never from the picker's name, so no renderer input can steer the write.
 */

import { randomUUID } from 'crypto';
import fs from 'fs';
import path from 'path';
import { writeFileAtomic } from '../../atomicFile';

import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import { requireComposerUser } from './composerSender';
import { parseInput } from '../schemas/error';
import {
  ACTION_DOCUMENT_EXTENSIONS,
  ActionAttachFileInputSchema,
  ActionDiscardAttachmentInputSchema,
  type ActionAttachFileResult,
} from '../schemas/actionAttachments';

/** The local backend reads staged files from this artifact-root-relative directory. */
const STAGED_ATTACHMENTS_DIRECTORY_SEGMENTS = ['generated', 'attachments'] as const;

/** `.tmp` is the artifact root's temp directory (`ARTIFACT_TEMP_DIRNAME` on the Python side). */
function attachmentTempDirectory(localArtifactRoot: string): string {
  return path.join(localArtifactRoot, '.tmp', 'generated-attachments');
}

function stagedAttachmentsDirectory(localArtifactRoot: string, userId: string): string {
  const root = path.join(localArtifactRoot, ...STAGED_ATTACHMENTS_DIRECTORY_SEGMENTS);
  const directory = path.join(root, userId);
  // The user id comes from the access token, so a value that is not one plain path segment is
  // a broken session rather than a rejectable attachment: refuse to touch the disk at all.
  if (path.dirname(directory) !== root || path.basename(directory) !== userId) {
    throw new Error('Refusing to stage an attachment outside the user attachment namespace.');
  }
  return directory;
}

/** Deletes a staged document; a file already gone (sent, or never written) is not an error. */
export function discardStagedAttachment(
  localArtifactRoot: string,
  userId: string,
  attachmentId: string
): void {
  const directory = stagedAttachmentsDirectory(localArtifactRoot, userId);
  // Only names `action:attachFile` could have written.
  for (const extension of ACTION_DOCUMENT_EXTENSIONS) {
    fs.rmSync(path.join(directory, `${attachmentId}${extension}`), { force: true });
  }
}

export function registerActionAttachmentHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  registrar.handle('action:attachFile', async (event, request) => {
    const userId = requireComposerUser(ctx, event.sender);
    const { bytes: payload, name: file } = parseInput(
      ActionAttachFileInputSchema,
      'action:attachFile',
      request
    );
    const localArtifactRoot = ctx.actionImages.localArtifactRoot;
    const attachmentId = randomUUID();
    const bytes = Buffer.from(payload);

    writeFileAtomic(
      path.join(
        stagedAttachmentsDirectory(localArtifactRoot, userId),
        `${attachmentId}${file.extension}`
      ),
      attachmentTempDirectory(localArtifactRoot),
      bytes
    );

    return {
      attachmentId,
      name: file.name,
      byteSize: bytes.byteLength,
    } satisfies ActionAttachFileResult;
  });

  registrar.handle('action:discardAttachment', async (event, request) => {
    const userId = requireComposerUser(ctx, event.sender);
    const { attachmentId } = parseInput(
      ActionDiscardAttachmentInputSchema,
      'action:discardAttachment',
      request
    );
    // A file a task's draft still lists, in this window or another, waits until none does.
    ctx.taskDrafts.discard(userId, attachmentId);
  });
}
