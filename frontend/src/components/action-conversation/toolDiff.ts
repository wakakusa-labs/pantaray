/**
 * apply_patch が返す 1 ファイル分の unified diff を、行番号つきの行に分ける。
 *
 * diff はバックエンドの create_patch_diff（difflib、前後 3 行の文脈、改行の無い最終行には
 * `\ No newline at end of file`）が作る。行の区切りもそちらに合わせて CR・LF・CRLF だけにする。
 */

export type DiffLineKind = 'context' | 'added' | 'removed';

export type DiffLine = Readonly<{
  kind: DiffLineKind;
  oldNumber: number | null;
  newNumber: number | null;
  text: string;
  /** ファイルがこの行で、改行を持たずに終わる。 */
  noNewlineAtEnd: boolean;
}>;

export type FileDiff = Readonly<{ path: string; hunks: readonly (readonly DiffLine[])[] }>;

const LINE_BREAK = /\r\n|\r|\n/;
const HUNK_HEADER = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;
const NEW_FILE_HEADER = '+++ ';
const LINE_KINDS: ReadonlyMap<string, DiffLineKind> = new Map([
  [' ', 'context'],
  ['+', 'added'],
  ['-', 'removed'],
]);
const NO_NEWLINE_MARKER = '\\';

/** 出力（JSON の文字列）が diff を持つときだけ、それを行に分ける。読めなければ null。 */
export function parseToolDiff(content: string): FileDiff | null {
  let output: unknown;
  try {
    output = JSON.parse(content);
  } catch (error) {
    if (error instanceof SyntaxError) return null;
    throw error;
  }
  if (typeof output !== 'object' || output === null || !('diff' in output)) return null;
  return typeof output.diff === 'string' ? parseUnifiedDiff(output.diff) : null;
}

/**
 * 見出しの行数と合わない hunk を持つ diff は null。改行の印を付ける前に保存された diff は、
 * 改行の無い行に次の行がつながっていて（`-b+c`）、そのまま描くと足した行が消える。
 */
export function parseUnifiedDiff(diff: string): FileDiff | null {
  const rows = diff.split(LINE_BREAK);
  if (rows[rows.length - 1] === '') rows.pop();
  let path: string | null = null;
  const hunks: DiffLine[][] = [];
  // 見出しが言う、まだ現れていない変更前・変更後の行数。
  let oldLeft = 0;
  let newLeft = 0;
  let oldNumber = 0;
  let newNumber = 0;
  for (const row of rows) {
    const header = HUNK_HEADER.exec(row);
    if (header !== null) {
      if (oldLeft !== 0 || newLeft !== 0) return null;
      oldNumber = Number(header[1]);
      newNumber = Number(header[3]);
      // 行数を省いた範囲は 1 行。
      oldLeft = Number(header[2] ?? 1);
      newLeft = Number(header[4] ?? 1);
      hunks.push([]);
      continue;
    }
    const hunk = hunks[hunks.length - 1];
    if (hunk === undefined) {
      // 最初の @@ より前はファイルの見出しだけ。@@ の後の `---` は削除された行。
      if (row.startsWith(NEW_FILE_HEADER)) path = row.slice(NEW_FILE_HEADER.length);
      continue;
    }
    if (row.startsWith(NO_NEWLINE_MARKER)) {
      const previous = hunk.pop();
      if (previous === undefined) return null;
      hunk.push({ ...previous, noNewlineAtEnd: true });
      continue;
    }
    const kind = LINE_KINDS.get(row.charAt(0));
    if (kind === undefined) return null;
    if (kind !== 'added') oldLeft -= 1;
    if (kind !== 'removed') newLeft -= 1;
    hunk.push({
      kind,
      oldNumber: kind === 'added' ? null : oldNumber++,
      newNumber: kind === 'removed' ? null : newNumber++,
      text: row.slice(1),
      noNewlineAtEnd: false,
    });
  }
  if (oldLeft !== 0 || newLeft !== 0) return null;
  return path === null || hunks.length === 0 ? null : { path, hunks };
}
