import { createContext } from 'react';

import type { ActionWorkRef } from '../../../electron/src/actions/actionContracts';
import type { WorkKey } from '@/components/chat/chatTimeline';
import type { MessageKey } from '@/i18n/types';

import type { RefSpan } from './ProjectRefText';

/**
 * Where a task or suggestion named in a sent message opens: the main window selects it in
 * History, the Overlay hands it to the main window. Without a host, the names do not open.
 */
export const OpenWorkContext = createContext<((work: WorkKey) => void) | null>(null);

/** A sent message's named projects and work; each task or suggestion opens when it can. */
export function sentMessageRefs(
  projectRefs: readonly RefSpan[],
  workRefs: readonly ActionWorkRef[],
  t: (key: MessageKey, vars?: Record<string, string | number>) => string,
  onOpenWork: ((work: WorkKey) => void) | null
): RefSpan[] {
  return [
    ...projectRefs,
    ...workRefs.map((ref) => ({
      start: ref.start,
      end: ref.end,
      open: onOpenWork
        ? {
            label: t('history.chat.workRef.open', { title: ref.title }),
            onOpen: () => onOpenWork(`${ref.kind}:${ref.id}`),
          }
        : undefined,
    })),
  ];
}
