import { useLayoutEffect, useRef } from 'react';

import { chipTitle, findWorkRefLinks, workRefLink, type WorkRefLink } from '@/history/workRefLink';
import {
  editedSpan,
  rebaseMentions,
  replaceWithText,
  type ComposerEdit,
  type ComposerMention,
  type WorkMention,
} from './composerMentions';

type Named = Omit<WorkMention, 'start' | 'end'>;
/** A link waiting for its read; `token` tells it apart however its span moves. */
type Waiting = WorkRefLink & Readonly<{ token: number }>;

// A character an id may go on with: a link followed by one is still being typed.
const ID_CHARACTER = /[A-Za-z0-9_-]/;

/** The chip a link names, read from History; null when History holds none for this user. */
function readNamed(link: WorkRefLink): Promise<Named | null> {
  const read = window.electron?.history?.readItem;
  if (!read) return Promise.resolve(null);
  return read({ kind: link.kind === 'action' ? 'conversation' : 'suggestion', id: link.id }).then(
    (row) =>
      row === null
        ? null
        : // A suggestion that became a task is that task now.
          row.kind === 'conversation'
          ? { kind: 'action', id: row.action_id, displayName: chipTitle(row.title) }
          : { kind: 'suggestion', id: row.suggestion_id, displayName: chipTitle(row.title) },
    // Unreadable is unresolved: the link stays the text the user wrote.
    () => null
  );
}

/**
 * Turns a task or suggestion link the user types or pastes into a chip titled from History. Only
 * links the user's own edit touched are read, so a chip turned back into its link stays text. The
 * answer may come after more typing: each waiting link is carried across edits, and the draft is
 * changed only where the link still stands complete.
 */
export function useWorkRefConversion({
  value,
  mentions,
  apply,
}: {
  value: string;
  mentions: readonly ComposerMention[];
  /** Applies an edit; `caret` is null when the field is not focused. */
  apply: (edit: ComposerEdit, caret: number | null) => void;
}) {
  // What a late answer edits: the draft as it is when the answer comes.
  const latest = useRef({ value, mentions });
  useLayoutEffect(() => {
    latest.current = { value, mentions };
  });
  const waiting = useRef<Waiting[]>([]);
  const tokens = useRef(0);
  const reads = useRef(new Map<string, Promise<Named | null>>());

  const convert = (link: Waiting, named: Named | null) => {
    const { value: draft, mentions: current } = latest.current;
    const stillThere = waiting.current.find((other) => other.token === link.token);
    waiting.current = waiting.current.filter((other) => other !== stillThere);
    if (
      !stillThere ||
      named === null ||
      draft.slice(stillThere.start, stillThere.end) !== workRefLink(link.kind, link.id) ||
      ID_CHARACTER.test(draft[stillThere.end] ?? '')
    ) {
      return;
    }
    const edit = replaceWithText(
      draft,
      current,
      stillThere.start,
      stillThere.end,
      named.displayName,
      named
    );
    const field = document.activeElement;
    const caret =
      field instanceof HTMLTextAreaElement && field.value === draft ? field.selectionStart : null;
    const delta = edit.draft.length - draft.length;
    // The next answer may come before the field draws this one: it works from this draft, with
    // the links still waiting moved as the replacement moved them.
    latest.current = { value: edit.draft, mentions: edit.mentions };
    waiting.current = waiting.current.flatMap((other) =>
      other.end <= stillThere.start
        ? [other]
        : other.start >= stillThere.end
          ? [{ ...other, start: other.start + delta, end: other.end + delta }]
          : []
    );
    apply(
      edit,
      caret === null
        ? null
        : caret >= stillThere.end
          ? caret + delta
          : Math.min(caret, stillThere.start + named.displayName.length)
    );
  };

  /** After the user's own edit from `previous` to `next`. */
  const noticeEdit = (previous: string, next: string, nextMentions: readonly ComposerMention[]) => {
    waiting.current = rebaseMentions(previous, next, waiting.current);
    const edited = editedSpan(previous, next);
    for (const link of findWorkRefLinks(next)) {
      const touched = link.start <= edited.end && edited.start <= link.end;
      const inChip = nextMentions.some((m) => m.start < link.end && link.start < m.end);
      if (!touched || inChip) continue;
      const entry = { ...link, token: (tokens.current += 1) };
      waiting.current = [...waiting.current, entry];
      const key = `${link.kind}:${link.id}`;
      const read = reads.current.get(key) ?? readNamed(link);
      reads.current.set(key, read);
      void read.then((named) => convert(entry, named));
    }
  };

  return { noticeEdit };
}
