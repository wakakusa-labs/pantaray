import { useRef } from 'react';

import { useI18n } from '@/context/useI18n';

import type { DiffLine, FileDiff } from './toolDiff';
import { useVerticalOverflow } from './useVerticalOverflow';

const COPY = {
  en: {
    oldLine: 'Old line',
    newLine: 'New line',
    change: 'Change',
    added: 'Added: ',
    removed: 'Removed: ',
    gap: 'Unchanged lines skipped',
    noNewline: 'No newline at end of file',
  },
  ja: {
    oldLine: '変更前の行',
    newLine: '変更後の行',
    change: '変更',
    added: '追加: ',
    removed: '削除: ',
    gap: '変更のない行を省略',
    noNewline: 'ファイル末尾に改行なし',
  },
} as const;

const SIGNS: Readonly<Record<DiffLine['kind'], string>> = {
  context: ' ',
  added: '+',
  removed: '−',
};

/**
 * 当てたパッチの差分。消した行は赤、足した行は緑で、色だけに頼らず読み上げにも追加・削除を
 * 言う。高さの上限は CSS が持ち、はみ出した分はこの箱の中だけを縦にスクロールさせる。
 */
export function ToolDiffView({ diff, label }: { diff: FileDiff; label: string }) {
  const { language } = useI18n();
  const copy = COPY[language];
  const ref = useRef<HTMLDivElement>(null);
  const scrollable = useVerticalOverflow(ref, diff);

  return (
    <div className="action-conversation__diff">
      <div className="action-conversation__diff-path">{diff.path}</div>
      <div
        ref={ref}
        className="action-conversation__diff-body"
        aria-label={label}
        role={scrollable ? 'group' : undefined}
        tabIndex={scrollable ? 0 : undefined}
      >
        <table className="action-conversation__diff-table">
          <thead className="action-conversation__sr-only">
            <tr>
              <th scope="col">{copy.oldLine}</th>
              <th scope="col">{copy.newLine}</th>
              <th scope="col">{copy.change}</th>
            </tr>
          </thead>
          {diff.hunks.map((hunk, hunkIndex) => (
            <tbody key={hunkIndex}>
              {hunkIndex > 0 ? (
                <tr className="action-conversation__diff-gap">
                  <td colSpan={3}>
                    <span aria-hidden>⋮</span>
                    <span className="action-conversation__sr-only">{copy.gap}</span>
                  </td>
                </tr>
              ) : null}
              {hunk.map((line, lineIndex) => (
                <tr
                  key={lineIndex}
                  className={`action-conversation__diff-line action-conversation__diff-line--${line.kind}`}
                >
                  <td className="action-conversation__diff-number">{line.oldNumber}</td>
                  <td className="action-conversation__diff-number">{line.newNumber}</td>
                  <td className="action-conversation__diff-text">
                    <span className="action-conversation__diff-sign" aria-hidden>
                      {SIGNS[line.kind]}
                    </span>
                    {line.kind === 'context' ? null : (
                      <span className="action-conversation__sr-only">{copy[line.kind]}</span>
                    )}
                    {line.text}
                    {line.noNewlineAtEnd ? (
                      <span className="action-conversation__diff-no-newline">{copy.noNewline}</span>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          ))}
        </table>
      </div>
    </div>
  );
}
