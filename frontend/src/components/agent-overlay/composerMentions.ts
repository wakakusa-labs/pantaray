import {
  codePointSpanInTrimmedText,
  type ActionProjectRef,
  type ActionWorkRef,
} from '../../../electron/src/actions/actionContracts';

/** A workspace project named with @. */
export type ProjectMention = Readonly<{
  kind: 'project';
  projectId: string;
  displayName: string;
  paths: readonly string[];
  start: number;
  end: number;
}>;

/** A task (`action`) or suggestion named by its chip; `displayName` is the chip's title. */
export type WorkMention = Readonly<{
  kind: 'action' | 'suggestion';
  id: string;
  displayName: string;
  start: number;
  end: number;
}>;

/**
 * Something named in the draft, drawn as a chip; `start`/`end` are UTF-16 offsets into the raw
 * draft, and the draft spells `displayName` there.
 */
export type ComposerMention = ProjectMention | WorkMention;

export type MentionTrigger = Readonly<{ start: number; query: string }>;

const TRIGGER_CHARACTERS = new Set(['@', '＠']);

/**
 * The `@` (or full-width `＠`) the caret is typing after, with the text typed since. Any
 * position on any line counts; whitespace between the trigger and the caret ends it.
 */
export function findMentionTrigger(draft: string, caret: number): MentionTrigger | null {
  for (let index = Math.min(caret, draft.length) - 1; index >= 0; index -= 1) {
    const character = draft[index];
    if (TRIGGER_CHARACTERS.has(character)) {
      return { start: index, query: draft.slice(index + 1, caret) };
    }
    if (/\s/.test(character)) return null;
  }
  return null;
}

function commonEnds(previous: string, next: string): { prefix: number; suffix: number } {
  const shorter = Math.min(previous.length, next.length);
  let prefix = 0;
  while (prefix < shorter && previous[prefix] === next[prefix]) prefix += 1;
  let suffix = 0;
  while (
    suffix < shorter - prefix &&
    previous[previous.length - 1 - suffix] === next[next.length - 1 - suffix]
  ) {
    suffix += 1;
  }
  return { prefix, suffix };
}

/** Where one edit put its text in `next`: the span between the common prefix and suffix. */
export function editedSpan(previous: string, next: string): { start: number; end: number } {
  const { prefix, suffix } = commonEnds(previous, next);
  return { start: prefix, end: next.length - suffix };
}

/**
 * Carry mentions across one edit (the span between common prefix and suffix): a mention it
 * touches becomes plain text, a later one shifts. Every kept mention still spells its name.
 */
export function rebaseMentions<T extends Readonly<{ start: number; end: number }>>(
  previous: string,
  next: string,
  mentions: readonly T[]
): T[] {
  if (previous === next) return [...mentions];
  const { prefix, suffix } = commonEnds(previous, next);
  const editedEnd = previous.length - suffix;
  const delta = next.length - previous.length;
  return mentions.flatMap((mention) => {
    if (mention.end <= prefix) return [mention];
    if (mention.start >= editedEnd) {
      return [{ ...mention, start: mention.start + delta, end: mention.end + delta }];
    }
    return [];
  });
}

export type ComposerEdit = Readonly<{
  draft: string;
  mentions: ComposerMention[];
  caret: number;
}>;

/** Replace the trigger and its query with the project name followed by one space. */
export function insertMention(
  draft: string,
  mentions: readonly ComposerMention[],
  trigger: MentionTrigger,
  project: Omit<ProjectMention, 'start' | 'end'>
): ComposerEdit {
  const queryEnd = trigger.start + 1 + trigger.query.length;
  const end = trigger.start + project.displayName.length;
  const next = `${draft.slice(0, trigger.start)}${project.displayName} ${draft.slice(queryEnd)}`;
  return {
    draft: next,
    mentions: [...rebaseMentions(draft, next, mentions), { ...project, start: trigger.start, end }]
      // The backend requires spans in text order.
      .sort((left, right) => left.start - right.start),
    caret: end + 1,
  };
}

/**
 * Replace `draft[start, end)` with `text`, which `named` (a task or suggestion) names when given;
 * the caret goes after it. A chip turning back into its link passes no `named`.
 */
export function replaceWithText(
  draft: string,
  mentions: readonly ComposerMention[],
  start: number,
  end: number,
  text: string,
  named: Omit<WorkMention, 'start' | 'end'> | null
): ComposerEdit {
  const next = draft.slice(0, start) + text + draft.slice(end);
  const delta = text.length - (end - start);
  // Not by diffing the drafts: a title can share its first letters with the link it replaces.
  const kept = mentions.flatMap((mention) =>
    mention.end <= start
      ? [mention]
      : mention.start >= end
        ? [{ ...mention, start: mention.start + delta, end: mention.end + delta }]
        : []
  );
  return {
    draft: next,
    mentions: named
      ? [...kept, { ...named, start, end: start + text.length }].sort(
          (left, right) => left.start - right.start
        )
      : kept,
    caret: start + text.length,
  };
}

/** Backspace right after a project name removes the whole name, not its last character. */
export function removeMentionBefore(
  draft: string,
  mentions: readonly ComposerMention[],
  caret: number
): ComposerEdit | null {
  const target = mentions.find((mention) => mention.end === caret);
  if (!target) return null;
  const next = draft.slice(0, target.start) + draft.slice(target.end);
  return {
    draft: next,
    mentions: rebaseMentions(draft, next, mentions),
    caret: target.start,
  };
}

/**
 * What the draft's mentions send: code-point spans into the trimmed text the backend stores,
 * projects and named work apart. The draft starts `draftOffset` UTF-16 units into the message
 * (after any quotes). The same conversion serves a message and a supplement.
 */
export function mentionRefs(
  message: Readonly<{ text: string; draftOffset: number }>,
  mentions: readonly ComposerMention[]
): { project_refs: ActionProjectRef[]; work_refs: ActionWorkRef[] } {
  const project_refs: ActionProjectRef[] = [];
  const work_refs: ActionWorkRef[] = [];
  for (const mention of mentions) {
    const span = codePointSpanInTrimmedText(
      message.text,
      mention.start + message.draftOffset,
      mention.end + message.draftOffset
    );
    if (mention.kind === 'project') {
      project_refs.push({
        project_id: mention.projectId,
        display_name: mention.displayName,
        paths: [...mention.paths],
        ...span,
      });
    } else {
      work_refs.push({ kind: mention.kind, id: mention.id, title: mention.displayName, ...span });
    }
  }
  return { project_refs, work_refs };
}

export function mentionOptionId(listId: string, index: number): string {
  return `${listId}-option-${index}`;
}
