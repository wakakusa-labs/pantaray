import {
  projectActionConversationView,
  type ActionConversationView,
} from '../../../electron/src/actions/actionConversationModel';
import { groupAgentWork } from '../action-conversation/actionWork';
import { useI18n } from '@/context/useI18n';
import { readWholeConversation } from './conversationPaging';
import { useClipboardCopy } from './useClipboardCopy';

type ActionsApi = NonNullable<NonNullable<typeof window.electron>['actions']>;

export type TranscriptCopy = Readonly<{
  user: string;
  pantaray: string;
  /** The line that stands for a message's attached images, which text cannot carry. */
  images: (count: number) => string;
}>;

/**
 * The conversation as the reader sees it, for pasting elsewhere: the approved
 * Suggestion the Overlay shows above the conversation, then every message outside
 * Pantaray's folded work, in display order, grouped exactly as the screen groups a
 * run. Answers keep their Markdown source.
 */
export function formatConversationTranscript(
  view: ActionConversationView,
  copy: TranscriptCopy
): string {
  const messages: string[] = [];
  const add = (speaker: string, lines: readonly (string | null)[]) => {
    const body = lines.filter((line) => line !== null);
    if (body.length > 0) messages.push([speaker, ...body].join('\n'));
  };
  const suggestion = view.action?.approved_suggestion;
  if (suggestion) add(copy.pantaray, [suggestion.content]);
  for (const item of view.items) {
    for (const line of item.kind === 'run' ? groupAgentWork(item.lines) : [item]) {
      if (line.kind === 'user') {
        const message = line.source === 'canonical' ? line.entry : line.submission.request.message;
        add(copy.user, [
          message.content,
          message.images.length > 0 ? copy.images(message.images.length) : null,
        ]);
      } else if (
        line.kind === 'assistant' ||
        line.kind === 'final_output' ||
        line.kind === 'terminal_outcome'
      ) {
        add(copy.pantaray, [line.text]);
      }
    }
  }
  return messages.join('\n\n');
}

/**
 * The header's conversation copy for the shown Action. It reads every page first, so
 * turns the Overlay has not loaded are included; a failed read copies nothing.
 */
export function useConversationCopy(
  actions: Pick<ActionsApi, 'readConversationPage'> | undefined,
  actionId: string | null
) {
  const { t } = useI18n();
  const { status, copy } = useClipboardCopy();
  if (actions === undefined || actionId === null) return undefined;
  return {
    status,
    copy: () =>
      void copy(async () => {
        const chain = await readWholeConversation(actions, actionId);
        return (
          chain &&
          formatConversationTranscript(projectActionConversationView(chain), {
            user: t('overlay.transcript.you'),
            pantaray: t('overlay.transcript.pantaray'),
            images: (count) =>
              t(count === 1 ? 'overlay.transcript.image' : 'overlay.transcript.images', {
                count,
              }),
          })
        );
      }),
  };
}
