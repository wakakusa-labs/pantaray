/**
 * The one text form of a reference to a task or a suggestion: `pantaray://task/<action_id>` and
 * `pantaray://suggestion/<suggestion_id>`. "Copy link" copies it, and every composer turns it into
 * a chip titled with the item's title. This file is the only place that writes or reads it.
 */

export type WorkRefKind = 'action' | 'suggestion';

export type WorkRefLink = Readonly<{
  kind: WorkRefKind;
  id: string;
  /** UTF-16 offsets of the whole link in the text it was found in. */
  start: number;
  end: number;
}>;

// The user calls an Action a task; the link says what they see.
const PATH_BY_KIND: Record<WorkRefKind, string> = { action: 'task', suggestion: 'suggestion' };
// Ids are UUIDs; a link ends at the first character no id holds.
const LINK_PATTERN = /pantaray:\/\/(task|suggestion)\/([A-Za-z0-9_-]+)/g;
// One line of a chip; a title is a request or a suggestion's whole text.
const CHIP_TITLE_MAX_CODE_POINTS = 50;

export function workRefLink(kind: WorkRefKind, id: string): string {
  return `pantaray://${PATH_BY_KIND[kind]}/${id}`;
}

export function findWorkRefLinks(text: string): WorkRefLink[] {
  return Array.from(text.matchAll(LINK_PATTERN), (match) => ({
    kind: match[1] === 'task' ? 'action' : 'suggestion',
    id: match[2],
    start: match.index,
    end: match.index + match[0].length,
  }));
}

/** What a chip shows of a title: its first line, cut to fit one line of the composer. */
export function chipTitle(title: string): string {
  const line = title
    .split('\n')
    .map((part) => part.trim())
    .find((part) => part !== '');
  const codePoints = Array.from(line ?? title.trim());
  return codePoints.length <= CHIP_TITLE_MAX_CODE_POINTS
    ? codePoints.join('')
    : `${codePoints.slice(0, CHIP_TITLE_MAX_CODE_POINTS - 1).join('')}…`;
}
