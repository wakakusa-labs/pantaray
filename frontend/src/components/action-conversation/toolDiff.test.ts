import { describe, expect, it } from 'vitest';

import { parseToolDiff, parseUnifiedDiff } from './toolDiff';

const numbered = (diff: string) =>
  parseUnifiedDiff(diff)?.hunks.map((hunk) =>
    hunk.map((line) => [line.kind, line.oldNumber, line.newNumber, line.text])
  );

describe('parseUnifiedDiff', () => {
  it('numbers old and new lines across two hunks', () => {
    const diff = [
      '--- notes.md',
      '+++ notes.md',
      '@@ -1,4 +1,4 @@',
      ' one',
      '-two',
      '+TWO',
      ' three',
      ' four',
      '@@ -10,3 +10,4 @@',
      ' ten',
      '+ten and a half',
      ' eleven',
      '--- not a header',
      '',
    ].join('\n');

    expect(parseUnifiedDiff(diff)?.path).toBe('notes.md');
    expect(numbered(diff)).toEqual([
      [
        ['context', 1, 1, 'one'],
        ['removed', 2, null, 'two'],
        ['added', null, 2, 'TWO'],
        ['context', 3, 3, 'three'],
        ['context', 4, 4, 'four'],
      ],
      [
        ['context', 10, 10, 'ten'],
        ['added', null, 11, 'ten and a half'],
        ['context', 11, 12, 'eleven'],
        ['removed', 12, null, '-- not a header'],
      ],
    ]);
  });

  it('marks the line a file ends on without a newline and keeps numbering', () => {
    const diff =
      '--- f.txt\n+++ f.txt\n@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+c\n\\ No newline at end of file\n';

    const lines = parseUnifiedDiff(diff)?.hunks[0];

    expect(
      lines?.map((line) => [line.kind, line.oldNumber, line.newNumber, line.noNewlineAtEnd])
    ).toEqual([
      ['context', 1, 1, false],
      ['removed', 2, null, true],
      ['added', null, 2, true],
    ]);
  });

  it('numbers a created file from its first line', () => {
    expect(numbered('--- n.txt\n+++ n.txt\n@@ -0,0 +1,2 @@\n+x\n+y\n')).toEqual([
      [
        ['added', null, 1, 'x'],
        ['added', null, 2, 'y'],
      ],
    ]);
  });
});

describe('parseToolDiff', () => {
  it('reads the diff of a patch result and nothing else', () => {
    const diff = '--- a\n+++ a\n@@ -1 +1 @@\n-x\n+y\n';

    expect(parseToolDiff(JSON.stringify({ status: 'success', diff }))?.hunks).toHaveLength(1);
    expect(parseToolDiff(JSON.stringify({ status: 'needs_read', text: 'x' }))).toBeNull();
    expect(parseToolDiff('{"status": "success", "diff": "--- a')).toBeNull();
    expect(parseToolDiff(JSON.stringify({ diff: '' }))).toBeNull();
  });
});
