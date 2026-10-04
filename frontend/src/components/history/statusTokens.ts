import type { MessageKey } from '@/i18n/types';

import type { ConversationHistoryStatus } from '../../../electron/src/history/historyContracts';

type BadgeTone = 'warning' | 'info';

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
