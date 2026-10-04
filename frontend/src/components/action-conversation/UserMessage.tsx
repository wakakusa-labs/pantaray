import type { ReactNode } from 'react';

import type { ActionConversationUserItem } from '../../../electron/src/actions/actionConversationModel';
import { useI18n } from '@/context/useI18n';
import { AttachedFileChip } from './AttachedFileChip';
import { AttachedImages, type ImageGridCopy } from './AttachedImages';

type Copy = {
  you: string;
  userImages: ImageGridCopy;
  userFiles: (count: number) => string;
  userStatus: Record<
    'pending' | 'not_executed' | 'submitting' | 'awaiting_refresh' | 'failed',
    string
  >;
};
const COPY: Record<'en' | 'ja', Copy> = {
  en: {
    you: 'You',
    userImages: {
      list: (count) => `${count} attached image${count === 1 ? '' : 's'}`,
      // The image content is unknown here, so the text describes the attachment, never its subject.
      imageAlt: (position, count) => `Attached image ${position} of ${count}`,
      open: (position, count) => `Open attached image ${position} of ${count}`,
      missing: 'Image unavailable',
      lightbox: {
        dialogLabel: 'Attached image',
        close: 'Close',
        reveal: 'Show in Finder',
      },
    },
    userFiles: (count) => `${count} attached file${count === 1 ? '' : 's'}`,
    userStatus: {
      pending: 'Pending',
      not_executed: 'Not executed',
      submitting: 'Sending',
      awaiting_refresh: 'Sent, updating',
      failed: 'Send failed',
    },
  },
  ja: {
    you: 'あなた',
    userImages: {
      list: (count) => `添付画像 ${count} 件`,
      imageAlt: (position, count) => `添付画像 ${position} / ${count}`,
      open: (position, count) => `添付画像 ${position} / ${count} を開く`,
      missing: '画像を表示できません',
      lightbox: {
        dialogLabel: '添付画像',
        close: '閉じる',
        reveal: 'Finder で表示',
      },
    },
    userFiles: (count) => `添付ファイル ${count} 件`,
    userStatus: {
      pending: '保留',
      not_executed: '未実行',
      submitting: '送信中',
      awaiting_refresh: '送信済み・更新中',
      failed: '送信失敗',
    },
  },
};

type ProjectRefSpan = Readonly<{ start: number; end: number }>;

/**
 * Split the text so each referenced workspace project renders in its own span.
 * Spans are Unicode code-point offsets (as the backend stores them), so the text is
 * indexed by code point rather than by UTF-16 unit. The backend guarantees the spans
 * are in order, do not overlap, and fall inside the text.
 */
function withProjectRefs(content: string, refs: readonly ProjectRefSpan[]): ReactNode {
  if (refs.length === 0) return content;
  const codePoints = Array.from(content);
  const parts: ReactNode[] = [];
  let cursor = 0;
  for (const ref of refs) {
    parts.push(codePoints.slice(cursor, ref.start).join(''));
    parts.push(
      <span key={ref.start} className="action-conversation__project-ref">
        {codePoints.slice(ref.start, ref.end).join('')}
      </span>
    );
    cursor = ref.end;
  }
  parts.push(codePoints.slice(cursor).join(''));
  return parts;
}

export function UserItem({ item }: { item: ActionConversationUserItem }) {
  const { language } = useI18n();
  const copy = COPY[language];
  const content =
    item.source === 'canonical' ? item.entry.content : item.submission.request.message.content;
  const status = item.source === 'canonical' ? item.entry.status : item.submission.state;
  const images =
    item.source === 'canonical' ? item.entry.images : item.submission.request.message.images;
  const files =
    (item.source === 'canonical' ? item.entry.files : item.submission.request.message.files) ?? [];
  const projectRefs =
    item.source === 'canonical'
      ? item.entry.project_refs
      : (item.submission.request.message.project_refs ?? []);
  const state =
    status !== 'adopted' ? (
      <span
        className="action-conversation__state"
        role={item.source === 'optimistic' ? 'status' : undefined}
      >
        {copy.userStatus[status]}
      </span>
    ) : null;

  if (content === null && images.length === 0 && files.length === 0) return state;
  return (
    <article className="action-conversation__user" aria-label={copy.you}>
      {content !== null ? <p>{withProjectRefs(content, projectRefs)}</p> : null}
      {images.length > 0 ? <AttachedImages images={images} copy={copy.userImages} /> : null}
      {files.length > 0 ? (
        <ul className="action-conversation__attachments" aria-label={copy.userFiles(files.length)}>
          {files.map((file, index) => (
            // A message may carry two files with the same name; the order is stable.
            <li key={index}>
              <AttachedFileChip name={file.name} byteSize={file.byte_size} />
            </li>
          ))}
        </ul>
      ) : null}
      {state}
    </article>
  );
}
