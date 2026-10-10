import { describe, expect, it } from 'vitest';

import { parseMultiFileDiff, parseToolDiff, parseUnifiedDiff } from './toolDiff';

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
      '@@ -10,3 +10,3 @@',
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

  it('numbers a hunk whose header omits one-line range lengths', () => {
    expect(numbered('--- a\n+++ a\n@@ -3 +3 @@\n-x\n+y\n')).toEqual([
      [
        ['removed', 3, null, 'x'],
        ['added', null, 3, 'y'],
      ],
    ]);
  });

  // 改行の印を付ける前に保存された diff。足した `c` が `-b` の行に隠れている。
  it('rejects a hunk whose lines disagree with its header', () => {
    expect(parseUnifiedDiff('--- f.txt\n+++ f.txt\n@@ -1,2 +1,2 @@\n a\n-b+c')).toBeNull();
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

describe('parseMultiFileDiff', () => {
  const summary = (text: string) =>
    parseMultiFileDiff(text)?.map((diff) => [
      diff.path,
      diff.hunks.flat().map((line) => `${line.kind}:${line.text}`),
    ]);

  it('splits a git diff at each file, naming a deleted file by its old path', () => {
    const diff = [
      'diff --git a/src/app.ts b/src/app.ts',
      'index 1111111..2222222 100644',
      '--- a/src/app.ts',
      '+++ b/src/app.ts',
      '@@ -1,2 +1,2 @@',
      ' keep',
      '-old',
      '+new',
      'diff --git a/old.md b/old.md',
      'deleted file mode 100644',
      '--- a/old.md',
      '+++ /dev/null',
      '@@ -1 +0,0 @@',
      '-gone',
      '',
    ].join('\n');

    expect(summary(diff)).toEqual([
      ['src/app.ts', ['context:keep', 'removed:old', 'added:new']],
      ['old.md', ['removed:gone']],
    ]);
  });

  it('splits a plain unified diff at each --- and +++ pair, dropping the file times', () => {
    const diff = [
      '--- a.txt\t2026-10-11 09:00:00 +0900',
      '+++ a.txt\t2026-10-11 09:05:00 +0900',
      '@@ -1 +1 @@',
      '-x',
      '+y',
      '--- b.txt',
      '+++ b.txt',
      '@@ -0,0 +1 @@',
      '+z',
    ].join('\r\n');

    expect(summary(diff)).toEqual([
      ['a.txt', ['removed:x', 'added:y']],
      ['b.txt', ['added:z']],
    ]);
  });

  it('reads nothing that is not a diff, or a diff one part of which does not read', () => {
    expect(parseMultiFileDiff('just some notes\n--- not a header\n')).toBeNull();
    expect(
      parseMultiFileDiff(
        [
          'diff --git a/a.txt b/a.txt',
          '--- a/a.txt',
          '+++ b/a.txt',
          '@@ -1 +1 @@',
          '-x',
          '+y',
          'diff --git a/logo.png b/logo.png',
          'Binary files a/logo.png and b/logo.png differ',
        ].join('\n')
      )
    ).toBeNull();
  });
});
