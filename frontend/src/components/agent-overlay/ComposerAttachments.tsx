import { useRef, type RefObject } from 'react';
import styled from 'styled-components';

import { useI18n } from '@/context/useI18n';
import { ACTION_MESSAGE_MAX_FILES } from '../../../electron/src/actions/actionContracts';
import { ACTION_IMAGE_MAX_PER_MESSAGE } from '../../../electron/src/ipc/schemas/actionImages';
import { buildActionImageUrl } from '../../../electron/src/protocol/imageStoragePath';
import { AttachedFileChip, FileChip } from '../action-conversation/AttachedFileChip';
import {
  attachmentKey,
  type AttachmentFailure,
  type ComposerAttachment,
} from './attachmentStaging';

const AttachmentList = styled.ul`
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
`;

const AttachmentItem = styled.li`
  position: relative;

  img {
    box-sizing: border-box;
    display: block;
    width: 96px;
    height: 96px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    object-fit: cover;
  }

  /* Room for the remove button, so it never covers the name. */
  ${FileChip}:not(:last-child) {
    padding-right: 32px;
  }
`;

const AttachmentRemoveButton = styled.button`
  position: absolute;
  top: 2px;
  right: 2px;
  width: 22px;
  height: 22px;
  padding: 0;
  border: 1px solid var(--border-color);
  border-radius: 50%;
  color: var(--text-primary);
  background: rgba(20, 24, 30, 0.9);
  font: inherit;
  line-height: 1;
  cursor: pointer;

  &:focus-visible {
    outline: 2px solid rgba(255, 255, 255, 0.7);
    outline-offset: 2px;
  }
`;

export const ComposerAlert = styled.span`
  color: rgba(254, 202, 202, 0.95);
  font-size: var(--text-meta-size);
`;

/**
 * What a composer will send with its message, each removable until it is sent, and why the
 * last file was refused. The Overlay's composer and the chat's show the same thing.
 */
export function ComposerAttachments({
  attachments,
  failure,
  readOnly,
  focusAfterLastRemoved,
  onRemove,
}: {
  attachments: readonly ComposerAttachment[];
  failure: AttachmentFailure | null;
  readOnly: boolean;
  focusAfterLastRemoved: RefObject<HTMLTextAreaElement>;
  onRemove: (attachment: ComposerAttachment) => void;
}) {
  const { t } = useI18n();
  const listRef = useRef<HTMLUListElement>(null);
  const images = attachments.filter((attachment) => attachment.kind === 'image');
  // The removed button leaves the page; focus moves to the neighbouring remove button, or back
  // to the message once nothing is attached, so a keyboard user is never dropped on <body>.
  const remove = (attachment: ComposerAttachment, index: number) => {
    const removeButtons = listRef.current?.querySelectorAll('button') ?? [];
    const next =
      removeButtons[index + 1] ?? removeButtons[index - 1] ?? focusAfterLastRemoved.current;
    onRemove(attachment);
    next?.focus();
  };
  return (
    <>
      {attachments.length > 0 ? (
        <AttachmentList
          ref={listRef}
          aria-label={t('overlay.composer.attachments', { count: attachments.length })}
        >
          {attachments.map((attachment, index) => (
            <AttachmentItem
              key={attachmentKey(attachment)}
              className="overlay-composer__attachment"
            >
              {attachment.kind === 'image' ? (
                <img
                  src={buildActionImageUrl(attachment.storagePath)}
                  alt={t('overlay.composer.attachmentAlt', {
                    index: images.indexOf(attachment) + 1,
                    count: images.length,
                  })}
                  loading="lazy"
                  decoding="async"
                  width={96}
                  height={96}
                />
              ) : (
                <AttachedFileChip name={attachment.name} byteSize={attachment.byteSize} />
              )}
              {!readOnly && (
                <AttachmentRemoveButton
                  type="button"
                  aria-label={
                    attachment.kind === 'image'
                      ? t('overlay.composer.attachmentRemove', {
                          index: images.indexOf(attachment) + 1,
                          count: images.length,
                        })
                      : t('overlay.composer.fileRemove', { name: attachment.name })
                  }
                  onClick={() => remove(attachment, index)}
                >
                  ×
                </AttachmentRemoveButton>
              )}
            </AttachmentItem>
          ))}
        </AttachmentList>
      ) : null}
      {failure ? (
        <ComposerAlert role="alert">
          {t(`overlay.composer.attachFailed.${failure}`, {
            limit:
              failure === 'too_many_files'
                ? ACTION_MESSAGE_MAX_FILES
                : ACTION_IMAGE_MAX_PER_MESSAGE,
          })}
        </ComposerAlert>
      ) : null}
    </>
  );
}
