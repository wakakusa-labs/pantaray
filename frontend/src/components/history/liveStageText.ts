import { useEffect, useRef, useState } from 'react';

import { resolveToolLine } from '@/components/action-conversation/toolDisplayName';
import { useI18n } from '@/context/useI18n';
import type { HistoryLiveStage } from '@/history/historyLiveStage';
import type { MessageKey } from '@/i18n/types';

/** How long a shown line stays before a newer one replaces it. Tools often finish in a blink. */
const HOLD_MS = 1200;
/** A quick hop between Tools is not worth a blink of "Thinking". */
const THINKING_DELAY_MS = 600;

/** A running Action's live line, worded the same in the sidebar row, the task pane, and its live work. */
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

/**
 * The live line, held steady: each shown line stays for HOLD_MS, a newer one that arrives during
 * the hold replaces any still waiting, and "Thinking" waits THINKING_DELAY_MS. When the stage
 * ends (`null`), the line ends at once, without a hold.
 */
export function useLiveStageText(stage: HistoryLiveStage | null): string | null {
  const { language, t } = useI18n();
  const text = stage === null ? null : liveStageText(stage, language, t);
  const delayMs = stage?.kind === 'thinking' ? THINKING_DELAY_MS : 0;
  const [shown, setShown] = useState<string | null>(null);
  const holdUntil = useRef(0);
  // With nothing on screen there is no hold: an ended stage clears and a step shows at once.
  if (text === null ? shown !== null : shown === null && delayMs === 0) setShown(text);

  useEffect(() => {
    holdUntil.current = shown === null ? 0 : Date.now() + HOLD_MS;
  }, [shown]);

  useEffect(() => {
    if (text === null || text === shown) return;
    const timer = setTimeout(
      () => setShown(text),
      Math.max(delayMs, holdUntil.current - Date.now())
    );
    return () => clearTimeout(timer);
  }, [text, delayMs, shown]);

  return text === null ? null : shown;
}
