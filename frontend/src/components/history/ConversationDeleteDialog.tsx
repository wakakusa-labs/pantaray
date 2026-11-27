import { useEffect, useState } from 'react';

import type { ConversationHistoryListItem } from '../../../electron/src/history/historyContracts';
import { useI18n } from '@/context/useI18n';

import { HistoryDeleteDialog } from './HistoryDeleteDialog';

/**
 * Asks before a conversation is deleted. Its schedules go with it, so the question counts them
 * first and opens once it knows; a count it cannot read is said as "any schedules".
 */
export function ConversationDeleteDialog({
  item,
  onConfirm,
  onCancel,
}: {
  item: ConversationHistoryListItem;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const { t } = useI18n();
  const actionId = item.kind === 'conversation' ? item.action_id : null;
  // A suggestion has no conversation of its own to hold schedules.
  const [schedules, setSchedules] = useState<number | 'unknown' | null>(
    actionId === null ? 0 : null
  );
  useEffect(() => {
    if (actionId === null) return;
    let live = true;
    const read = window.electron?.hooks?.list() ?? Promise.reject(new Error('No bridge.'));
    read.then(
      (list) => {
        if (!live) return;
        setSchedules(
          list.hooks.filter(
            (hook) => hook.created_from_action_id === actionId || hook.target_action_id === actionId
          ).length
        );
      },
      () => {
        if (live) setSchedules('unknown');
      }
    );
    return () => {
      live = false;
    };
  }, [actionId]);

  if (schedules === null) return null;
  return (
    <HistoryDeleteDialog
      t={t}
      title={t('history.delete.confirmTitle')}
      body={
        schedules === 'unknown'
          ? t('history.delete.confirmBodyMaybeSchedules')
          : schedules === 0
            ? t('history.delete.confirmBody')
            : t(
                schedules === 1
                  ? 'history.delete.confirmBodySchedules.one'
                  : 'history.delete.confirmBodySchedules.other',
                { count: schedules }
              )
      }
      onConfirm={onConfirm}
      onCancel={onCancel}
    />
  );
}
