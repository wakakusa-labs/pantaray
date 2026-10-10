import { resolveToolLine } from '@/components/action-conversation/toolDisplayName';
import type { HistoryLiveStage } from '@/history/historyLiveStage';
import type { MessageKey } from '@/i18n/types';

/** A running Action's live line, worded the same in the sidebar row and the task pane. */
export function liveStageText(
  stage: HistoryLiveStage,
  language: 'en' | 'ja',
  t: (key: MessageKey) => string
): string {
  switch (stage.kind) {
    case 'tool':
      return resolveToolLine(stage.label, language, {
        subject: stage.subject,
        running: true,
        outcome: stage.outcome,
      }).text;
    case 'message':
      return stage.text;
    case 'thinking':
      return t('overlay.thinking');
  }
}
