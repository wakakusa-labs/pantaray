// Quotes travel as the message's own text, each passage a Markdown blockquote followed by the
// comment: models read it as is, the backend stores plain text, and a relayed message keeps it.

/** A passage the user picked, with what they want to say about it (possibly nothing). */
export type ComposerQuote = Readonly<{ id: string; text: string; comment: string }>;

// The passage as it is quoted: lines kept, trailing spaces and the blank lines around it dropped.
// Null when nothing but whitespace was picked.
export function quotedPassage(selected: string): string | null {
  const passage = selected
    .replace(/\r\n?/g, '\n')
    .split('\n')
    .map((line) => line.trimEnd())
    .join('\n')
    .replace(/^\n+|\n+$/g, '');
  return passage.trim() === '' ? null : passage;
}

function blockquote(passage: string): string {
  return passage
    .split('\n')
    .map((line) => (line === '' ? '>' : `> ${line}`))
    .join('\n');
}

// The text a message carries: each quote in the order added, then the draft. `draftOffset` is
// where the draft starts in it, in UTF-16 units, so spans into the draft (its @ mentions) can be
// moved into the message. With no quotes the draft goes out as it is.
export function composeQuotedMessage(
  quotes: readonly ComposerQuote[],
  draft: string
): Readonly<{ text: string; draftOffset: number }> {
  if (quotes.length === 0) return { text: draft, draftOffset: 0 };
  // A blank line ends a blockquote; without it the next line would read as part of the quote.
  const prefix = quotes
    .map((quote) => {
      const comment = quote.comment.trim();
      return comment === '' ? blockquote(quote.text) : `${blockquote(quote.text)}\n\n${comment}`;
    })
    .join('\n\n');
  if (draft.trim() === '') return { text: prefix, draftOffset: prefix.length };
  const head = `${prefix}\n\n`;
  return { text: head + draft, draftOffset: head.length };
}

export type QuotedTextBlock = Readonly<
  | { kind: 'quote'; text: string }
  /** `start` is the block's offset in the message, in code points, as project ref spans are. */
  | { kind: 'text'; text: string; start: number }
>;

// A Markdown blockquote line: up to three spaces, `>`, and an optional space that is not content.
const QUOTE_LINE = /^ {0,3}> ?/;

// A sent message split into its quoted passages and the user's own words, in order, for drawing.
// Blank lines between blocks only separate them and are dropped.
export function splitQuotedText(text: string): QuotedTextBlock[] {
  const blocks: QuotedTextBlock[] = [];
  let lines: string[] = [];
  let kind: 'quote' | 'text' = 'text';
  let start = 0;
  let offset = 0;
  const flush = () => {
    if (kind === 'quote') {
      blocks.push({ kind, text: lines.join('\n') });
    } else {
      let first = 0;
      let blockStart = start;
      while (first < lines.length && lines[first].trim() === '') {
        blockStart += Array.from(lines[first]).length + 1;
        first += 1;
      }
      let last = lines.length;
      while (last > first && lines[last - 1].trim() === '') last -= 1;
      if (last > first) {
        blocks.push({ kind, text: lines.slice(first, last).join('\n'), start: blockStart });
      }
    }
    lines = [];
  };
  for (const line of text.split('\n')) {
    const marker = QUOTE_LINE.exec(line);
    const lineKind = marker ? 'quote' : 'text';
    if (lineKind !== kind) {
      flush();
      kind = lineKind;
      start = offset;
    }
    lines.push(marker ? line.slice(marker[0].length) : line);
    offset += Array.from(line).length + 1;
  }
  flush();
  return blocks;
}
