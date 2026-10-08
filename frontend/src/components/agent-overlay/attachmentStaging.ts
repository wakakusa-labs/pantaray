import {
  ACTION_MESSAGE_MAX_FILES,
  type ActionFileAttachment,
} from '../../../electron/src/actions/actionContracts';
import {
  ACTION_IMAGE_MAX_BYTES,
  ACTION_IMAGE_MAX_PER_MESSAGE,
  type ActionImageAttachRejectionReason,
} from '../../../electron/src/ipc/schemas/actionImages';
import {
  ACTION_DOCUMENT_EXTENSIONS,
  ACTION_DOCUMENT_MAX_BYTES,
  actionDocumentExtension,
} from '../../../electron/src/ipc/schemas/actionAttachments';
import {
  ACTION_IMAGE_MIME_TYPES,
  type ActionImageMimeType,
} from '../../../electron/src/protocol/imageStoragePath';

/**
 * Why the composer refused a file. Image reasons come from the image limits, `file_*` from the
 * document limits; `failed` covers a broken IPC round trip.
 */
export type AttachmentFailure =
  | ActionImageAttachRejectionReason
  | 'too_many'
  | 'file_too_large'
  | 'too_many_files'
  | 'failed';

/** An image written to the artifact root, or a document staged for the next message. */
export type ComposerAttachment =
  | { kind: 'image'; storagePath: string }
  | { kind: 'file'; attachmentId: string; name: string; byteSize: number };

export type AttachmentActions = Pick<
  NonNullable<NonNullable<typeof window.electron>['actions']>,
  'attachImage' | 'attachFile' | 'discardAttachment'
>;

/** What the picker offers; dropped and pasted files go through the same checks. */
export const ATTACHMENT_ACCEPT = [...ACTION_IMAGE_MIME_TYPES, ...ACTION_DOCUMENT_EXTENSIONS].join(
  ','
);

export function attachmentKey(attachment: ComposerAttachment): string {
  return attachment.kind === 'image' ? attachment.storagePath : attachment.attachmentId;
}

function isActionImageMimeType(value: string): value is ActionImageMimeType {
  return (ACTION_IMAGE_MIME_TYPES as readonly string[]).includes(value);
}

/** Per-message limits, and the refusal each one produces when reached. */
const ATTACHMENT_LIMITS = {
  image: { max: ACTION_IMAGE_MAX_PER_MESSAGE, failure: 'too_many' },
  file: { max: ACTION_MESSAGE_MAX_FILES, failure: 'too_many_files' },
} as const;

function countAttachments(
  attachments: readonly ComposerAttachment[],
  kind: ComposerAttachment['kind']
): number {
  return attachments.filter((attachment) => attachment.kind === kind).length;
}

export function canAttachMore(attachments: readonly ComposerAttachment[]): boolean {
  return (['image', 'file'] as const).some(
    (kind) => countAttachments(attachments, kind) < ATTACHMENT_LIMITS[kind].max
  );
}

/** The attachments as a message carries them. */
export function attachmentPayload(attachments: readonly ComposerAttachment[]): {
  images: { kind: 'image'; storage_path: string }[];
  files: ActionFileAttachment[];
} {
  return {
    images: attachments.flatMap((attachment) =>
      attachment.kind === 'image'
        ? [{ kind: 'image' as const, storage_path: attachment.storagePath }]
        : []
    ),
    files: attachments.flatMap((attachment) =>
      attachment.kind === 'file'
        ? [
            {
              attachment_id: attachment.attachmentId,
              name: attachment.name,
              byte_size: attachment.byteSize,
            },
          ]
        : []
    ),
  };
}

/**
 * Removes staged documents that will never be sent. A staged file left behind only takes disk
 * space until the planned sweep, so a failed discard is not reported to the user.
 */
export function discardDocuments(
  actions: AttachmentActions | undefined,
  attachments: readonly ComposerAttachment[]
): void {
  for (const attachment of attachments) {
    if (attachment.kind !== 'file') continue;
    actions?.discardAttachment({ attachmentId: attachment.attachmentId }).catch(() => undefined);
  }
}

/** Writes one file through Electron main; an image the main process declines yields its reason. */
async function writeAttachment(
  actions: AttachmentActions,
  file: File
): Promise<ComposerAttachment | ActionImageAttachRejectionReason> {
  const bytes = await file.arrayBuffer();
  if (isActionImageMimeType(file.type)) {
    const result = await actions.attachImage({ bytes, declaredMimeType: file.type });
    return result.kind === 'rejected'
      ? result.reason
      : { kind: 'image', storagePath: result.storagePath };
  }
  const { attachmentId, name, byteSize } = await actions.attachFile({ bytes, name: file.name });
  return { kind: 'file', attachmentId, name, byteSize };
}

/**
 * Files are written by Electron main one at a time and the first refusal stops the batch, so the
 * user is told exactly which file was the problem instead of getting a partial result with no
 * explanation. Type, size and count are checked here first: all are known from the File, and
 * sending megabytes across IPC only to have them refused is pure waste.
 */
export async function stageFiles(
  actions: AttachmentActions,
  picked: readonly File[],
  existing: readonly ComposerAttachment[]
): Promise<{ accepted: ComposerAttachment[]; failure: AttachmentFailure | null }> {
  const accepted: ComposerAttachment[] = [];
  let failure: AttachmentFailure | null = null;
  for (const file of picked) {
    // Documents go by extension: Chromium leaves `type` empty for a notebook.
    const kind = isActionImageMimeType(file.type)
      ? 'image'
      : actionDocumentExtension(file.name) !== null
        ? 'file'
        : null;
    if (kind === null) {
      failure = 'unsupported_media_type';
    } else if (countAttachments([...existing, ...accepted], kind) >= ATTACHMENT_LIMITS[kind].max) {
      failure = ATTACHMENT_LIMITS[kind].failure;
    } else if (kind === 'image' && file.size > ACTION_IMAGE_MAX_BYTES) {
      failure = 'too_large';
    } else if (kind === 'file' && file.size > ACTION_DOCUMENT_MAX_BYTES) {
      failure = 'file_too_large';
    }
    if (failure) break;
    let result;
    try {
      result = await writeAttachment(actions, file);
    } catch {
      failure = 'failed';
      break;
    }
    if (typeof result === 'string') {
      failure = result;
      break;
    }
    accepted.push(result);
  }
  return { accepted, failure };
}

/**
 * Adds a finished batch to the attachments as they are now: another batch may have landed while
 * this one was writing, so the limits are checked again and the overflow is discarded.
 */
export function addStagedAttachments(
  actions: AttachmentActions | undefined,
  current: readonly ComposerAttachment[],
  accepted: readonly ComposerAttachment[],
  failure: AttachmentFailure | null
): { attachments: readonly ComposerAttachment[]; failure: AttachmentFailure | null } {
  let attachments = current;
  let attachmentFailure = failure;
  for (const attachment of accepted) {
    const limit = ATTACHMENT_LIMITS[attachment.kind];
    if (countAttachments(attachments, attachment.kind) < limit.max) {
      attachments = [...attachments, attachment];
    } else {
      attachmentFailure = limit.failure;
      // Discarding by id is idempotent, so a repeated updater call does no harm.
      discardDocuments(actions, [attachment]);
    }
  }
  return { attachments, failure: attachmentFailure };
}
