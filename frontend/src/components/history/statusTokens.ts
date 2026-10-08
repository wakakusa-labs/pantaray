import type { MessageKey } from '@/i18n/types';

import type {
  ConversationHistoryListItem,
  ConversationHistoryStatus,
} from '../../../electron/src/history/historyContracts';

type BadgeTone = 'warning' | 'info' | null;

export const badgeClassName = (tone: BadgeTone) => (tone ? `badge badge--${tone}` : 'badge');

/**
 * 履歴一覧のバッジは「いま注意が必要か」だけを表す。`idle` は会話が走っていない
 * ことしか意味せず、完了・失敗・中断を区別できないため、バッジを出さない。
 */
export const getConversationHistoryStatusMeta = (
  status: ConversationHistoryStatus
): { labelKey: MessageKey; tone: BadgeTone } | null => {
  switch (status) {
    case 'running':
      return { labelKey: 'history.status.running', tone: 'info' };
    case 'approval_pending':
      return { labelKey: 'history.status.approvalPending', tone: 'warning' };
    case 'idle':
      return null;
  }
};

/**
 * A list row's badge. A Suggestion row says it is a suggestion, brighter while it still waits for
 * the user's answer, so it is not mistaken for an Action waiting for approval.
 */
export const getHistoryItemStatusMeta = (
  item: ConversationHistoryListItem
): { labelKey: MessageKey; tone: BadgeTone } | null =>
  item.kind === 'suggestion'
    ? {
        labelKey: 'history.status.suggestion',
        tone: item.status === 'approval_pending' ? 'warning' : null,
      }
    : getConversationHistoryStatusMeta(item.status);
