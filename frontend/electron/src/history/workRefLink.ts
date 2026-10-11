/**
 * The one text form of a reference to a task or a suggestion: `pantaray://task/<action_id>` and
 * `pantaray://suggestion/<suggestion_id>`. "Copy link" copies it, every composer turns it into a
 * chip titled with the item's title, and a link opened from another app shows the item in the
 * main window. This file is the only place that writes or reads it.
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

/** The item a whole URL names, or null when the URL is anything but exactly one link. */
export function parseWorkRefUrl(url: string): { kind: WorkRefKind; id: string } | null {
  const [link, ...rest] = findWorkRefLinks(url);
  if (!link || rest.length > 0 || link.start !== 0 || link.end !== url.length) return null;
  return { kind: link.kind, id: link.id };
}
