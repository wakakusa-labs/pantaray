import { useLayoutEffect, useMemo, useRef, useState } from 'react';

import type { ActionConversationToolItem } from '../../../electron/src/actions/actionConversationModel';
import type { ActionImageReference } from '../../../electron/src/actions/actionContracts';
import type {
  ActionToolOutputKey,
  ActionToolOutputLoader,
  LoadedActionToolOutput,
} from '../../../electron/src/actions/actionToolOutputLoader';

import { useI18n } from '@/context/useI18n';

import { AttachedImages, type ImageGridCopy } from './AttachedImages';
import { ToolDiffView } from './ToolDiffView';
import { ToolOutputText } from './ToolOutputText';
import { parseToolDiff } from './toolDiff';
import type { ToolDisplay, ToolLine } from './toolDisplayName';

type FileEdit = NonNullable<ActionConversationToolItem['entry']['file_edit']>;

type LoadState =
  | Readonly<{ kind: 'idle' }>
  | Readonly<{ kind: 'loading' }>
  | Readonly<{ kind: 'failed' }>
  | Readonly<{ kind: 'loaded'; output: LoadedActionToolOutput }>;

const COPY = {
  en: {
    output: 'Tool output',
    loading: 'Loading output',
    loaded: 'Tool output loaded.',
    failed: 'Could not load tool output.',
    retry: 'Retry',
    step: (number: number) => `Step ${number}`,
    added: (count: number) => `${count} line${count === 1 ? '' : 's'} added`,
    removed: (count: number) => `${count} line${count === 1 ? '' : 's'} removed`,
    truncated: 'Output is truncated.',
    unavailable: {
      no_output: 'No output is available.',
      binary: 'Binary output cannot be displayed.',
    },
  },
  ja: {
    output: 'ツール出力',
    loading: 'ツール出力を読み込み中',
    loaded: 'ツール出力を読み込みました。',
    failed: 'ツール出力を読み込めませんでした。',
    retry: '再試行',
    step: (number: number) => `ステップ ${number}`,
    added: (count: number) => `${count} 行追加`,
    removed: (count: number) => `${count} 行削除`,
    truncated: '出力は途中まで表示されています。',
    unavailable: {
      no_output: '表示できる出力はありません。',
      binary: 'バイナリ出力は表示できません。',
    },
  },
} as const;

/**
 * 実行中と失敗だけが状態語を持つ。終わったツールは行そのものが結果なのでラベルを持たない。
 * `failed` は失敗を色でも示すためのもので、文言は呼び出し側が言語ごとに決める。
 */
export type ToolRowStatus = Readonly<{ label: string; failed: boolean }>;

function ToolRowStatusLabel({
  status,
  announce,
}: {
  status: ToolRowStatus | null;
  announce: boolean;
}) {
  if (status === null) return null;
  return (
    <span
      className={
        status.failed
          ? 'action-conversation__state action-conversation__state--failed'
          : 'action-conversation__state'
      }
      role={announce ? 'status' : undefined}
    >
      {status.label}
    </span>
  );
}

/**
 * ツール 1 回分の行。行そのものが「何をしたか」の 1 文で、そのまま開閉ボタンでもある。
 * 開くと、その回が残した画像と出力が下にぶら下がる。開くものが何もない行はボタンにしない。
 */
export function ToolRow({
  Icon,
  line,
  preview,
  fileEdit,
  stepNumber,
  runLabel,
  status,
  announceStatus,
  shimmer,
  images,
  imagesCopy,
  outputKey,
  loader,
  onFocusedContentUnmount,
}: {
  Icon: ToolDisplay['Icon'];
  line: ToolLine;
  preview: string | null;
  /** 当てたパッチの行数。見出しの文の後ろに +N −M で添える。 */
  fileEdit: FileEdit | null;
  stepNumber: number;
  runLabel: string;
  status: ToolRowStatus | null;
  announceStatus: boolean;
  /** Set while this Tool is running, so its line shows the progress shimmer. */
  shimmer: boolean;
  images: readonly ActionImageReference[];
  imagesCopy: ImageGridCopy;
  outputKey: ActionToolOutputKey | null;
  loader: ActionToolOutputLoader;
  onFocusedContentUnmount: () => void;
}) {
  const { language } = useI18n();
  const copy = COPY[language];
  const rootRef = useRef<HTMLDivElement>(null);
  const disclosureRef = useRef<HTMLButtonElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [state, setState] = useState<LoadState>({ kind: 'idle' });
  // 同じ 1 文の行が 1 回の実行に複数並びうるので、読み上げ名は歩数で見分けられるようにする。
  const rowLabel = `${line.text}, ${copy.step(stepNumber)}, ${runLabel}`;
  const outputLabel = `${copy.output}, ${rowLabel}`;
  // 行数は 0 でない方だけを言う。作ったファイルに「0 行削除」は要らない。
  const counts =
    fileEdit === null
      ? []
      : [
          { kind: 'added', sign: '+', count: fileEdit.added_lines, text: copy.added },
          { kind: 'removed', sign: '−', count: fileEdit.removed_lines, text: copy.removed },
        ].filter((part) => part.count > 0);
  const countsLabel =
    counts.length > 0 ? counts.map((part) => part.text(part.count)).join(', ') : null;
  // 開閉ボタンの読み上げ名は子孫の文字を覆い隠すので、目に見えている結果の頭と状態語も名前に
  // 含める。含めないと、焦点を当てた利用者だけが失敗にも結果にも気づけない。
  const disclosureLabel = [
    line.text,
    countsLabel,
    preview,
    status?.label,
    copy.step(stepNumber),
    runLabel,
  ]
    .filter((part) => part !== null && part !== undefined)
    .join(', ');
  const expandable = outputKey !== null || images.length > 0;
  // 途中で切れた出力は JSON として読めないので、diff を探さずにそのまま見せる。
  const diff = useMemo(
    () =>
      state.kind === 'loaded' && state.output.kind === 'text' && !state.output.truncated
        ? parseToolDiff(state.output.content)
        : null,
    [state]
  );

  useLayoutEffect(
    () => () => {
      if (rootRef.current?.contains(document.activeElement)) onFocusedContentUnmount();
    },
    [onFocusedContentUnmount]
  );

  const settle = (load: Promise<LoadedActionToolOutput>) => {
    setState({ kind: 'loading' });
    void load.then(
      (output) => setState({ kind: 'loaded', output }),
      () => setState({ kind: 'failed' })
    );
  };

  const toggle = () => {
    const nextExpanded = !expanded;
    setExpanded(nextExpanded);
    if (nextExpanded && state.kind === 'idle' && outputKey !== null) settle(loader.load(outputKey));
  };

  const retry = () => {
    if (outputKey === null) return;
    disclosureRef.current?.focus();
    settle(loader.retry(outputKey));
  };

  const content = (
    <>
      <Icon className="action-conversation__tool-icon" size={14} aria-hidden />
      <span
        className={[
          'action-conversation__tool-text',
          line.mono && 'action-conversation__tool-text--mono',
          shimmer && 'action-conversation__shimmer',
        ]
          .filter(Boolean)
          .join(' ')}
      >
        {line.text}
      </span>
      {countsLabel !== null ? (
        <span className="action-conversation__diff-stat">
          {counts.map((part) => (
            <span
              key={part.kind}
              className={`action-conversation__diff-stat-${part.kind}`}
              aria-hidden
            >
              {part.sign}
              {part.count}
            </span>
          ))}
          <span className="action-conversation__sr-only">{countsLabel}</span>
        </span>
      ) : null}
      {preview !== null ? (
        <span className="action-conversation__tool-preview">{preview}</span>
      ) : null}
    </>
  );

  return (
    <div ref={rootRef} className="action-conversation__tool">
      {/*
       * 状態語は行の本体の外に置く。出力を持って失敗した行は、その 1 回の更新で開けるように
       * なって本体が div からボタンに変わる。本体の中に置くと読み上げ領域ごと作り直され、
       * 最初から「失敗」を抱えた状態で現れた領域は読み上げられないことが多い。
       */}
      <div className="action-conversation__tool-line">
        {expandable ? (
          <button
            ref={disclosureRef}
            className="action-conversation__tool-line-body action-conversation__disclosure"
            type="button"
            aria-label={disclosureLabel}
            aria-expanded={expanded}
            onClick={toggle}
          >
            {content}
          </button>
        ) : (
          <span className="action-conversation__tool-line-body">{content}</span>
        )}
        <ToolRowStatusLabel status={status} announce={announceStatus} />
      </div>
      {expandable && expanded ? (
        <div className="action-conversation__tool-output-detail">
          {/* 畳んだ行は 1 行のまま。撮ったもの・返ってきたものはこの中にだけ置く。 */}
          {images.length > 0 ? <AttachedImages images={images} copy={imagesCopy} /> : null}
          {outputKey === null ? null : state.kind === 'idle' || state.kind === 'loading' ? (
            <span className="action-conversation__state" role="status">
              {copy.loading}
            </span>
          ) : state.kind === 'failed' ? (
            <div className="action-conversation__tool-output-failure">
              <span role="alert">{copy.failed}</span>
              <button type="button" aria-label={`${copy.retry}, ${rowLabel}`} onClick={retry}>
                {copy.retry}
              </button>
            </div>
          ) : state.output.kind === 'unavailable' ? (
            <span className="action-conversation__state" role="status">
              {copy.unavailable[state.output.reason]}
            </span>
          ) : (
            <>
              <span className="action-conversation__sr-only" role="status">
                {state.output.truncated ? `${copy.loaded} ${copy.truncated}` : copy.loaded}
              </span>
              {diff !== null ? (
                <ToolDiffView diff={diff} label={outputLabel} />
              ) : (
                <ToolOutputText content={state.output.content} label={outputLabel} />
              )}
              {state.output.truncated ? (
                <span className="action-conversation__state">{copy.truncated}</span>
              ) : null}
            </>
          )}
        </div>
      ) : null}
    </div>
  );
}
