/**
 * Composer image attachments (`action:attachImage`) and "reveal in Finder" (`actionImage:reveal`).
 *
 * Electron main owns `LOCAL_ARTIFACT_ROOT` and already writes screenshot artifacts under it, so
 * user attachments are written here directly instead of being pushed through the local backend:
 * no extra trust boundary, and the 8 MB payload is not serialized a second time. The Python
 * reader still re-validates everything it is handed (`local_image_store.py`), so nothing here is
 * trusted downstream.
 *
 * No metadata sidecar is written: the MIME type follows from the extension and the sha256 is
 * recomputed on read, so the "image and metadata disagree" failure mode does not exist.
 */

import { createHash, randomUUID } from 'crypto';
import path from 'path';
import { writeFileAtomic } from '../../atomicFile';

import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import { requireComposerUser } from './composerSender';
import { parseInput } from '../schemas/error';
import {
  ACTION_IMAGE_MAX_BYTES,
  ACTION_IMAGE_MAX_DECODED_PIXELS,
  ActionImageAttachInputSchema,
  ActionImageRevealInputSchema,
  type ActionImageAttachResult,
} from '../schemas/actionImages';
import { readVerifiedStoredImage, storedImageAbsolutePath } from '../../protocol/imageProtocol';
import {
  IMAGE_EXTENSION_BY_MIME_TYPE,
  imageStorageDateSegment,
  isValidImageStoragePath,
  sniffImageMimeType,
} from '../../protocol/imageStoragePath';

/** `.tmp` is the artifact root's temp directory (`ARTIFACT_TEMP_DIRNAME` on the Python side). */
function attachmentTempDirectory(localArtifactRoot: string): string {
  return path.join(localArtifactRoot, '.tmp', 'generated-images');
}

export function registerActionImageHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  registrar.handle('action:attachImage', async (event, request) => {
    const userId = requireComposerUser(ctx, event.sender);

    const input = parseInput(ActionImageAttachInputSchema, 'action:attachImage', request);
    if (input.bytes.byteLength > ACTION_IMAGE_MAX_BYTES) {
      return { kind: 'rejected', reason: 'too_large' } satisfies ActionImageAttachResult;
    }
    const bytes = Buffer.from(input.bytes);

    // The declared type is renderer metadata; the bytes decide. A mismatch means the file is not
    // what the picker said it was, so it is refused rather than silently relabelled.
    const mimeType = sniffImageMimeType(bytes);
    if (mimeType === null || mimeType !== input.declaredMimeType) {
      return {
        kind: 'rejected',
        reason: 'unsupported_media_type',
      } satisfies ActionImageAttachResult;
    }

    const dimensions = ctx.actionImages.decodeImageDimensions(bytes);
    if (
      dimensions === null ||
      dimensions.widthPx <= 0 ||
      dimensions.heightPx <= 0 ||
      dimensions.widthPx * dimensions.heightPx > ACTION_IMAGE_MAX_DECODED_PIXELS
    ) {
      return { kind: 'rejected', reason: 'decode_failed' } satisfies ActionImageAttachResult;
    }

    const localArtifactRoot = ctx.actionImages.localArtifactRoot;
    const storagePath = `${userId}/${imageStorageDateSegment(new Date())}/${randomUUID()}${
      IMAGE_EXTENSION_BY_MIME_TYPE[mimeType]
    }`;
    // The user id comes from the access token, so a value that cannot form a storage path is a
    // broken session rather than a rejectable attachment: refuse to write anywhere at all.
    if (!isValidImageStoragePath({ userId, storagePath })) {
      throw new Error('Refusing to store an image outside the user image namespace.');
    }

    writeFileAtomic(
      storedImageAbsolutePath(localArtifactRoot, storagePath),
      attachmentTempDirectory(localArtifactRoot),
      bytes
    );

    return {
      kind: 'attached',
      storagePath,
      mimeType,
      byteSize: bytes.byteLength,
      sha256: createHash('sha256').update(bytes).digest('hex'),
      widthPx: dimensions.widthPx,
      heightPx: dimensions.heightPx,
    } satisfies ActionImageAttachResult;
  });

  // The chat in the main window shows the same attached images as the Overlay does.
  registrar.handle('actionImage:reveal', async (event, request) => {
    requireComposerUser(ctx, event.sender);
    const parsed = parseInput(ActionImageRevealInputSchema, 'actionImage:reveal', request);
    const image = readVerifiedStoredImage(
      {
        localArtifactRoot: ctx.actionImages.localArtifactRoot,
        getCurrentSubjectId: ctx.actions.getCurrentSubjectId,
      },
      parsed.storagePath
    );
    if (image === null) return { revealed: false } as const;
    ctx.actionImages.revealInFolder(image.absolutePath);
    return { revealed: true } as const;
  });
}
