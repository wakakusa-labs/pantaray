import { describe, expect, it } from 'vitest';
import { composeQuotedMessage } from './messageQuotes';
import {
  findMentionTrigger,
  insertMention,
  mentionRefs,
  rebaseMentions,
  removeMentionBefore,
  type ComposerMention,
} from './composerMentions';

const aurora = {
  kind: 'project' as const,
  projectId: 'p-aurora',
  displayName: 'Aurora',
  paths: ['/Users/demo/aurora'],
};

function mention(start: number, displayName = 'Aurora'): ComposerMention {
  return { ...aurora, displayName, start, end: start + displayName.length };
}

describe('findMentionTrigger', () => {
  it('opens after @ anywhere on any line, including full-width ＠ glued to text', () => {
    expect(findMentionTrigger('mail@au', 7)).toEqual({ start: 4, query: 'au' });
    expect(findMentionTrigger('first line\n＠北極', 14)).toEqual({ start: 11, query: '北極' });
  });

  it('closes on whitespace between the trigger and the caret, or a caret before it', () => {
    expect(findMentionTrigger('@au ra', 6)).toBeNull();
    expect(findMentionTrigger('@au\nra', 6)).toBeNull();
    expect(findMentionTrigger('ab@cd', 2)).toBeNull();
  });
});

describe('rebaseMentions', () => {
  const draft = 'see Aurora now';
  const mentions = [mention(4)];

  it('shifts a mention after an edit before it and keeps it when typing right after it', () => {
    expect(rebaseMentions(draft, `please ${draft}`, mentions)).toEqual([mention(11)]);
    expect(rebaseMentions(draft, 'see Aurora, now', mentions)).toEqual(mentions);
  });

  it('turns a mention back into text when the edit touches it', () => {
    expect(rebaseMentions(draft, 'see Aurxora now', mentions)).toEqual([]);
    expect(rebaseMentions(draft, 'see Auror now', mentions)).toEqual([]);
    expect(rebaseMentions(draft, 'see now', mentions)).toEqual([]);
  });
});

describe('insertMention', () => {
  it('replaces @ and the query with the name and one space, keeping spans ordered', () => {
    const draft = 'x @au y Aurora';
    const edit = insertMention(draft, [mention(8)], { start: 2, query: 'au' }, aurora);
    expect(edit.draft).toBe('x Aurora  y Aurora');
    expect(edit.caret).toBe(9);
    expect(edit.mentions).toEqual([mention(2), mention(12)]);
  });
});

describe('removeMentionBefore', () => {
  it('removes the whole name right before the caret and shifts later mentions', () => {
    const edit = removeMentionBefore('Aurora and Nimbus', [mention(0), mention(11, 'Nimbus')], 6);
    expect(edit).toEqual({ draft: ' and Nimbus', mentions: [mention(5, 'Nimbus')], caret: 0 });
    expect(removeMentionBefore('Aurora ', [mention(0)], 7)).toBeNull();
  });
});

describe('mentionRefs', () => {
  it('sends projects and named work apart, as code-point spans into the trimmed text', () => {
    // The emoji is two UTF-16 units but one code point; the leading spaces are trimmed off.
    const draft = '  😀 Aurora vs Q3 report';
    const refs = mentionRefs({ text: draft, draftOffset: 0 }, [
      mention(5),
      { kind: 'action', id: 'a-1', displayName: 'Q3 report', start: 15, end: 24 },
    ]);
    expect(refs).toEqual({
      project_refs: [
        {
          project_id: 'p-aurora',
          display_name: 'Aurora',
          paths: ['/Users/demo/aurora'],
          start: 2,
          end: 8,
        },
      ],
      work_refs: [{ kind: 'action', id: 'a-1', title: 'Q3 report', start: 12, end: 21 }],
    });
  });

  it('moves the spans past the quotes the message opens with', () => {
    const draft = 'Q3 を見て';
    const message = composeQuotedMessage([{ id: 'q1', text: '前回の答え', comment: '' }], draft);
    const refs = mentionRefs(message, [
      { kind: 'action', id: 'a-1', displayName: 'Q3', start: 0, end: 2 },
    ]);
    const [ref] = refs.work_refs;
    expect(Array.from(message.text).slice(ref.start, ref.end).join('')).toBe('Q3');
  });
});
