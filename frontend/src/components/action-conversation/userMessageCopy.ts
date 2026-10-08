import type { ImageGridCopy } from './AttachedImages';

type UserMessageCopy = {
  you: string;
  /** Names what Pantaray's chat wrote when it handed work to the task. */
  chatNote: string;
  userImages: ImageGridCopy;
  userFiles: (count: number) => string;
  userStatus: Record<
    'pending' | 'not_executed' | 'submitting' | 'awaiting_refresh' | 'failed',
    string
  >;
};
/** The labels of a user's message and its attachments, shared by the Overlay and the chat. */
export const USER_MESSAGE_COPY: Record<'en' | 'ja', UserMessageCopy> = {
  en: {
    you: 'You',
    chatNote: 'From the chat',
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
    chatNote: 'チャットから',
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
