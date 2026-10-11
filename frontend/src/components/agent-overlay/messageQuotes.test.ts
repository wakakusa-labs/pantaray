import { expect, it } from 'vitest';

import { composeQuotedMessage, quotedPassage, splitQuotedText } from './messageQuotes';

const quote = (text: string, comment = '') => ({ id: text, text, comment });

it('writes each quote as a blockquote with its comment, in order, before the draft', () => {
  const message = composeQuotedMessage(
    [quote('first line\n\nsecond', 'too long'), quote('B plan')],
    'Also, @Aurora'
  );
  expect(message.text).toBe('> first line\n>\n> second\n\ntoo long\n\n> B plan\n\nAlso, @Aurora');
  expect(message.text.slice(message.draftOffset)).toBe('Also, @Aurora');
});

it('sends the draft untouched without quotes, and the quotes alone without a draft', () => {
  expect(composeQuotedMessage([], '  hi ')).toEqual({ text: '  hi ', draftOffset: 0 });
  expect(composeQuotedMessage([quote('A', ' ok ')], '  ').text).toBe('> A\n\nok');
});

it('keeps the lines, drops trailing spaces and surrounding blank lines, refuses blanks', () => {
  expect(quotedPassage('\r\n  one  \r\ntwo\n\n')).toBe('  one\ntwo');
  expect(quotedPassage(' \n\t')).toBeNull();
});

it('reads a composed message back as its quotes and words, with code-point starts', () => {
  const { text } = composeQuotedMessage([quote('🙂 A\n\nB', 'why')], 'see Aurora');
  expect(splitQuotedText(text)).toEqual([
    { kind: 'quote', text: '🙂 A\n\nB' },
    { kind: 'text', text: 'why\n\nsee Aurora', start: 13 },
  ]);
  expect(Array.from(text).slice(13).join('')).toBe('why\n\nsee Aurora');
});

it('leaves a message without quotes as one block of words', () => {
  expect(splitQuotedText('a\n\nb')).toEqual([{ kind: 'text', text: 'a\n\nb', start: 0 }]);
});
