import { useRef } from 'react';
import type { ReactNode, RefObject } from 'react';
import styled from 'styled-components';
import { ArrowUp, Play, Plus, Square } from 'lucide-react';

import { useI18n } from '@/context/useI18n';
import {
  ACTION_IMAGE_MIME_TYPES,
  buildActionImageUrl,
} from '../../../electron/src/protocol/imageStoragePath';
import { ACTION_DOCUMENT_EXTENSIONS } from '../../../electron/src/ipc/schemas/actionAttachments';
import { ACTION_IMAGE_MAX_PER_MESSAGE } from '../../../electron/src/ipc/schemas/actionImages';
import { ACTION_MESSAGE_MAX_FILES } from '../../../electron/src/actions/actionContracts';
import { AttachedFileChip, FileChip } from '../action-conversation/AttachedFileChip';
import { ApprovalModeMenu } from './ApprovalModeMenu';
import { ComposerMessageField } from './ComposerMessageField';
import type { ComposerMention } from './composerMentions';
import type { ActionApprovalModeControl } from './useActionApprovalMode';
import {
  attachmentKey,
  type AttachmentFailure,
  type ComposerAttachment,
  type ComposerState,
} from './useOverlayComposerController';

/**
 * 入力欄と、その入力に対する操作をひとつにまとめた枠。
 *
 * 本文欄・添付のサムネイル・操作の行が同じ面の上に載る。操作を枠の外へ出すと、
 * 押した先がどの入力に効くのかを位置関係だけで察することになるので、この面が
 * 「ここに書いて、ここから送る」という範囲そのものを示す。
 *
 * 高さは中身が決める。本文欄が伸びれば枠も上へ伸び、操作の行は常に下端に残る。
 */
const ComposerForm = styled.form`
  display: grid;
  gap: 8px;
  padding: 8px;
  border: 1px solid var(--border-color);
  border-radius: 16px;
  /*
   * 枠線はガラスの上ではほとんど見えないので、入力欄であることはこの面が示す。
   */
  background: rgba(10, 14, 20, 0.34);

  /*
   * 本文欄は枠いっぱいに広がるので、自前の輪郭を出すと枠と二重になる。焦点の印は
   * 枠そのものが持つ。行の操作（追加・権限・送信）は各自の輪郭を出すため、
   * :focus-within ではなく本文欄だけを見る。
   */
  &:has(textarea:focus-visible) {
    outline: 2px solid rgba(255, 255, 255, 0.7);
    outline-offset: -1px;
  }
`;

const ComposerAlert = styled.span`
  color: rgba(254, 202, 202, 0.95);
  font-size: var(--text-meta-size);
`;

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

/** 枠の下端に残る操作の行。追加と権限が左に並び、送信だけが反対の端へ寄る。 */
const ComposerActions = styled.div`
  display: flex;
  align-items: center;
  gap: 4px;
`;

/**
 * ファイル追加のアイコンボタン。
 *
 * 枠も面も持たない。同じ行に並ぶ操作権限のピルと送信の円が面を持つので、ここまで
 * 面を敷くと下端が板の列になる。存在はホバー／フォーカス時の薄い灰だけで示す。
 * 当たり判定は 32px 角で、行の他の操作と高さを揃える。
 */
export const ComposerIconButton = styled.button`
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  padding: 0;
  border: 0;
  border-radius: 8px;
  color: var(--text-primary);
  background: transparent;
  cursor: pointer;
  transition: background-color 120ms ease;

  svg {
    display: block;
    width: 18px;
    height: 18px;
  }

  &:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.08);
  }

  &:focus-visible {
    outline: 2px solid rgba(255, 255, 255, 0.7);
    outline-offset: 2px;
  }

  &:disabled {
    opacity: 0.4;
    cursor: default;
  }
`;

/**
 * 行の右端に置く唯一の塗られた円。
 *
 * 枠の中で最も明るい面にすることで、他の操作と違って「ここで確定する」ことが
 * 色だけで分かる。担う操作は入力の状態で決まる（本文があれば送信、なければ
 * 動いているものの停止か、直前に止めたものの再開）が、位置も大きさも変えない。
 * 押せない間は不透明度だけを落とす。
 */
const ComposerPrimaryButton = styled.button`
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  padding: 0;
  border: 0;
  /* 行の他の操作から離し、枠の右端へ寄せる。 */
  margin-left: auto;
  border-radius: 50%;
  color: #12161c;
  background: rgba(255, 255, 255, 0.92);
  cursor: pointer;
  transition: background-color 120ms ease;

  svg {
    display: block;
    width: 18px;
    height: 18px;
  }

  &:hover:not(:disabled) {
    background: rgb(255, 255, 255);
  }

  &:focus-visible {
    outline: 2px solid rgba(255, 255, 255, 0.7);
    outline-offset: 2px;
  }

  &:disabled {
    opacity: 0.4;
    cursor: default;
  }
`;

/** What the picker offers; dropped and pasted files go through the same controller checks. */
const ATTACH_ACCEPT = [...ACTION_IMAGE_MIME_TYPES, ...ACTION_DOCUMENT_EXTENSIONS].join(',');

const MESSAGE_FIELD_ID = 'overlay-composer-message';
const MESSAGE_ERROR_ID = 'overlay-composer-message-error';

const ScreenReaderOnly = styled.label`
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
`;

type OverlayComposerProps = {
  approvalMode: ActionApprovalModeControl;
  draft: string;
  mentions: readonly ComposerMention[];
  /** Keep the unconfirmed message selectable while its exact request is retried. */
  submissionControls: ReactNode;
  retryAcceptance: boolean;
  attachments: readonly ComposerAttachment[];
  /** 直前の添付が拒否された理由。拒否がなければ null。 */
  attachmentFailure: AttachmentFailure | null;
  validationFailed: boolean;
  canAttach: boolean;
  /**
   * 右端のボタンが担う操作。本文があれば送信、空でエージェントが動いていれば
   * 停止、空で直前に止めたままなら再開。
   */
  action: 'send' | 'stop' | 'resume' | 'accept';
  canSend: boolean;
  /** 「再開」の要求が通らなかった。次の入力で消える。 */
  resumeFailed: boolean;
  canResume: boolean;
  textareaRef: RefObject<HTMLTextAreaElement>;
  onDraftChange: (value: string, mentions: ComposerMention[]) => void;
  onAttachFiles: (files: readonly File[]) => void;
  onRemoveAttachment: (attachment: ComposerAttachment) => void;
  onSubmit: () => void;
  onStop: () => void;
  onResume: () => void;
};

/** Submission controls stay in the input frame; Stop remains available while a run is live. */
export const ComposerSubmissionRow = styled.div`
  min-height: 32px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-sm);
`;

/** The one stop control. A live run is stopped from here and nowhere else. */
export function ComposerStopButton({ onStop }: { onStop: () => void }) {
  const { t } = useI18n();
  return (
    <ComposerPrimaryButton
      type="button"
      aria-label={t('overlay.composer.stop')}
      title={t('overlay.composer.stop')}
      onClick={onStop}
    >
      <Square
        style={{ width: 12, height: 12 }}
        strokeWidth={2.25}
        fill="currentColor"
        aria-hidden
      />
    </ComposerPrimaryButton>
  );
}

export function ComposerSubmissionStatus({
  composer,
  controlRef,
  onRetry,
  onRefresh,
  onStop,
}: {
  composer: Pick<ComposerState, 'submission' | 'failureKind' | 'refreshState'>;
  controlRef: RefObject<HTMLButtonElement>;
  onRetry: () => void;
  onRefresh: (button: HTMLButtonElement) => void;
  onStop?: () => void;
}) {
  const { t } = useI18n();
  const { submission, failureKind, refreshState } = composer;
  if (!submission) return null;
  return (
    <ComposerSubmissionRow>
      {submission.state === 'failed' && failureKind === 'transport' ? (
        <button
          ref={controlRef}
          className="action-conversation__retry"
          type="button"
          onClick={onRetry}
        >
          {t('overlay.composer.retry')}
        </button>
      ) : submission.state === 'awaiting_refresh' || failureKind === 'expected_process_conflict' ? (
        <button
          ref={controlRef}
          className="action-conversation__retry"
          type="button"
          aria-disabled={refreshState === 'loading'}
          aria-live={refreshState === 'failed' ? 'assertive' : undefined}
          onClick={(event) => onRefresh(event.currentTarget)}
        >
          {t(`overlay.composer.refresh.${refreshState}`)}
        </button>
      ) : failureKind === 'action_conflict' ? (
        <p className="action-conversation__state" role="alert">
          {t('overlay.composer.actionConflict')}
        </p>
      ) : (
        <span role="status">{t('overlay.composer.sending')}</span>
      )}
      {onStop && <ComposerStopButton onStop={onStop} />}
    </ComposerSubmissionRow>
  );
}

export function OverlayComposer({
  approvalMode,
  draft,
  mentions,
  submissionControls,
  retryAcceptance,
  attachments,
  attachmentFailure,
  validationFailed,
  canAttach,
  action,
  canSend,
  resumeFailed,
  canResume,
  textareaRef,
  onDraftChange,
  onAttachFiles,
  onRemoveAttachment,
  onSubmit,
  onStop,
  onResume,
}: OverlayComposerProps) {
  const { t } = useI18n();
  const attachInputRef = useRef<HTMLInputElement>(null);
  const attachmentListRef = useRef<HTMLUListElement>(null);
  const isReadOnly = retryAcceptance || Boolean(submissionControls);
  const images = attachments.filter((attachment) => attachment.kind === 'image');
  // The removed button leaves the page; focus moves to the neighbouring remove button, or back
  // to the message once nothing is attached, so a keyboard user is never dropped on <body>.
  const removeAttachment = (attachment: ComposerAttachment, index: number) => {
    const removeButtons = attachmentListRef.current?.querySelectorAll('button') ?? [];
    const next = removeButtons[index + 1] ?? removeButtons[index - 1] ?? textareaRef.current;
    onRemoveAttachment(attachment);
    next?.focus();
  };

  return (
    <ComposerForm
      className="overlay-composer"
      onSubmit={(event) => {
        event.preventDefault();
        if (canSend) onSubmit();
      }}
      onDragOver={(event) => event.preventDefault()}
      onDrop={(event) => {
        event.preventDefault();
        if (!isReadOnly) onAttachFiles(Array.from(event.dataTransfer.files));
      }}
    >
      <ScreenReaderOnly htmlFor={MESSAGE_FIELD_ID}>
        {t(action === 'accept' ? 'overlay.supplement.label' : 'overlay.composer.label')}
      </ScreenReaderOnly>
      <ComposerMessageField
        id={MESSAGE_FIELD_ID}
        textareaRef={textareaRef}
        value={draft}
        mentions={mentions}
        readOnly={isReadOnly}
        placeholder={t(
          action === 'accept' ? 'overlay.supplement.placeholder' : 'overlay.composer.placeholder'
        )}
        invalid={validationFailed}
        describedBy={validationFailed ? MESSAGE_ERROR_ID : undefined}
        onChange={onDraftChange}
        onKeyDown={(event) => {
          if (event.key !== 'Enter' || event.shiftKey) return;
          // 改行は Shift+Enter だけが入れる。送れない状態でも Enter で改行させない。
          event.preventDefault();
          if (canSend) onSubmit();
        }}
        onPaste={(event) => {
          const files = Array.from(event.clipboardData.files);
          if (isReadOnly || files.length === 0) return;
          // A pasted screenshot also arrives as text/plain noise in some apps; taking the
          // files means the textarea must not additionally insert that text.
          event.preventDefault();
          onAttachFiles(files);
        }}
      />
      {attachments.length > 0 ? (
        <AttachmentList
          ref={attachmentListRef}
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
              {!isReadOnly && (
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
                  onClick={() => removeAttachment(attachment, index)}
                >
                  ×
                </AttachmentRemoveButton>
              )}
            </AttachmentItem>
          ))}
        </AttachmentList>
      ) : null}
      {attachmentFailure ? (
        <ComposerAlert role="alert">
          {t(`overlay.composer.attachFailed.${attachmentFailure}`, {
            limit:
              attachmentFailure === 'too_many_files'
                ? ACTION_MESSAGE_MAX_FILES
                : ACTION_IMAGE_MAX_PER_MESSAGE,
          })}
        </ComposerAlert>
      ) : null}
      {validationFailed ? (
        <ComposerAlert id={MESSAGE_ERROR_ID} role="alert">
          {t(action === 'accept' ? 'overlay.supplement.invalid' : 'overlay.composer.invalid')}
        </ComposerAlert>
      ) : null}
      {resumeFailed ? (
        <ComposerAlert role="alert">{t('overlay.composer.resumeFailed')}</ComposerAlert>
      ) : null}
      {submissionControls ??
        (!retryAcceptance && (
          <ComposerActions>
            <input
              ref={attachInputRef}
              type="file"
              hidden
              multiple
              accept={ATTACH_ACCEPT}
              onChange={(event) => {
                const files = Array.from(event.target.files ?? []);
                // Reset so re-picking the same file still fires a change event.
                event.target.value = '';
                onAttachFiles(files);
              }}
            />
            <ComposerIconButton
              type="button"
              disabled={!canAttach}
              aria-label={t('overlay.composer.attach')}
              title={t('overlay.composer.attach')}
              onClick={() => attachInputRef.current?.click()}
            >
              <Plus strokeWidth={1.75} aria-hidden />
            </ComposerIconButton>
            <ApprovalModeMenu approvalMode={approvalMode} />
            {action === 'send' ? (
              <ComposerPrimaryButton
                type="submit"
                disabled={!canSend}
                aria-label={t('overlay.composer.send')}
                title={t('overlay.composer.send')}
              >
                <ArrowUp strokeWidth={2.25} aria-hidden />
              </ComposerPrimaryButton>
            ) : action === 'stop' ? (
              <ComposerStopButton onStop={onStop} />
            ) : action === 'resume' ? (
              <ComposerPrimaryButton
                type="button"
                disabled={!canResume}
                aria-label={t('overlay.composer.resume')}
                title={t('overlay.composer.resume')}
                onClick={onResume}
              >
                <Play strokeWidth={2.25} fill="currentColor" aria-hidden />
              </ComposerPrimaryButton>
            ) : null}
          </ComposerActions>
        ))}
    </ComposerForm>
  );
}
