import type {
  ActionConversationAssistantItem,
  ActionConversationRunLine,
  ActionConversationToolItem,
} from '../../../electron/src/actions/actionConversationModel';
import { resolveToolDisplay, type ToolDisplay } from './toolDisplayName';

type WorkLine = ActionConversationAssistantItem | ActionConversationToolItem;
export type AgentWorkSection = {
  kind: 'agent_work';
  key: string;
  lines: [WorkLine, ...WorkLine[]];
};

/**
 * A run folds its commentary into Pantaray's work once its final answer is shown; until
 * then, and in a run that failed or stopped, the commentary stays in the conversation.
 */
export function foldsCommentary(lines: readonly ActionConversationRunLine[]): boolean {
  return lines.some((line) => line.kind === 'final_output');
}

/**
 * Keys of the commentary a reply without tool calls became the final answer from; the answer
 * already shows that text. The reply is the run's last commentary, its messages joined by a
 * blank line.
 */
function commentaryRepeatingAnswer(lines: readonly ActionConversationRunLine[]): Set<string> {
  const answerIndex = lines.findIndex((line) => line.kind === 'final_output');
  const answer = lines[answerIndex];
  if (answer?.kind !== 'final_output') return new Set();
  const reply: ActionConversationAssistantItem[] = [];
  for (const line of lines.slice(0, answerIndex).reverse()) {
    if (line.kind !== 'assistant') break;
    reply.unshift(line);
  }
  for (; reply.length > 0; reply.shift()) {
    if (
      reply
        .map((line) => line.text)
        .join('\n\n')
        .trim() === answer.text.trim()
    ) {
      return new Set(reply.map((line) => line.key));
    }
  }
  return new Set();
}

/** Group only adjacent work; user messages and final answers always remain boundaries. */
export function groupAgentWork(
  lines: readonly ActionConversationRunLine[]
): (ActionConversationRunLine | AgentWorkSection)[] {
  const foldCommentary = foldsCommentary(lines);
  const repeatsAnswer = commentaryRepeatingAnswer(lines);
  const sections: (ActionConversationRunLine | AgentWorkSection)[] = [];
  // A message-only Suggestion precedes the initial USER and remains conversation context.
  const initialUserIndex = lines.findIndex(
    (line) =>
      line.kind === 'user' && line.source === 'canonical' && line.entry.accepted_sequence === 1
  );
  for (const [index, line] of lines.entries()) {
    if (line.kind === 'assistant' && repeatsAnswer.has(line.key)) continue;
    if (
      line.kind === 'tool' ||
      (foldCommentary && line.kind === 'assistant' && index > initialUserIndex)
    ) {
      const previous = sections[sections.length - 1];
      if (previous?.kind === 'agent_work') previous.lines.push(line);
      else sections.push({ kind: 'agent_work', key: line.key, lines: [line] });
    } else {
      sections.push(line);
    }
  }
  return sections;
}

export type ToolGroupLine = {
  kind: 'tool_group';
  key: string;
  toolKey: string;
  display: ToolDisplay;
  lines: ActionConversationToolItem[];
};

function groupable(
  line: ActionConversationRunLine | ToolGroupLine | undefined
): line is ActionConversationToolItem {
  return (
    line?.kind === 'tool' && line.entry.status === 'success' && line.entry.outcome === 'completed'
  );
}

/** Only settled work groups consecutive completed tools; failures and commentary break a group. */
export function groupRunLines(
  lines: readonly ActionConversationRunLine[],
  language: 'en' | 'ja'
): readonly (ActionConversationRunLine | ToolGroupLine)[] {
  const grouped: (ActionConversationRunLine | ToolGroupLine)[] = [];
  for (const line of lines) {
    if (!groupable(line)) {
      grouped.push(line);
      continue;
    }
    const display = resolveToolDisplay(line.entry.label, language);
    const previous = grouped[grouped.length - 1];
    if (previous?.kind === 'tool_group' && previous.toolKey === display.key) {
      previous.lines.push(line);
      continue;
    }
    if (
      groupable(previous) &&
      resolveToolDisplay(previous.entry.label, language).key === display.key
    ) {
      grouped[grouped.length - 1] = {
        kind: 'tool_group',
        key: previous.key,
        toolKey: display.key,
        display,
        lines: [previous, line],
      };
      continue;
    }
    grouped.push(line);
  }
  return grouped;
}
