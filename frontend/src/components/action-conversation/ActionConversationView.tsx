import { useCallback, useLayoutEffect, useRef, useState } from 'react';

import type {
  ActionConversationRunItem,
  ActionConversationRunLine,
  ActionConversationToolItem,
  ActionConversationView as ActionConversationViewModel,
} from '../../../electron/src/actions/actionConversationModel';
import type { ActionLiveSnapshot } from '../../../electron/src/actions/actionLiveCore';
import type { ActionToolOutputLoader } from '../../../electron/src/actions/actionToolOutputLoader';

import { MarkdownBlock } from '@/components/agent-overlay/MarkdownRenderer';
import { useI18n } from '@/context/useI18n';

import type { ImageGridCopy } from './AttachedImages';
import { UserItem } from './UserMessage';
import { CopyAnswerButton } from './CopyAnswerButton';
import { ToolRow, type ToolRowStatus } from './ToolRow';
import { resolveToolDisplay, resolveToolLine } from './toolDisplayName';
import {
  foldsCommentary,
  groupAgentWork,
  groupRunLines,
  type AgentWorkSection,
  type ToolGroupLine,
} from './actionWork';

import './actionConversationView.css';

/**
 * 実行が続いている間だけ状態を持つ。終了状態（完了 / 失敗 / キャンセル）は返答そのものが結果なので
 * 状態語を持たない。
 */
type ActiveRunStatus = Extract<ActionConversationRunItem['status'], 'running' | 'approval_pending'>;

type Copy = {
  conversation: string;
  agentWork: string;
  section: string;
  step: string;
  run: string;
  assistant: string;
  toolImages: ImageGridCopy;
  finalAnswer: string;
  failure: string;
  canceled: string;
  // 画面には出さず、読み上げにだけ渡す実行状態。
  runStatus: Record<ActiveRunStatus, string>;
  // 終わったツールは状態語を持たない。実行中と失敗だけがラベルを持つ。
  toolStatus: Record<LabeledToolStatus, string>;
  // 呼ばれたのに起きなかったツール。失敗ではないので、赤くはせず印だけ残す。
  toolNotRun: string;
  toolResultUnavailable: string;
  // 承認を待って止まっているツール。動いてはいないので「実行中」とは出さない。
  toolAwaitingApproval: string;
};

type ToolEntry = ActionConversationToolItem['entry'];
type ToolStatus = ToolEntry['status'];
/** 成功した終了状態は状態語を持たない。返答そのものが結果なので「完了」は出さない。 */
type LabeledToolStatus = Exclude<ToolStatus, 'success'>;

/**
 * 承認されなかった呼び出しと、記録がオフのまま呼ばれた読み取りは、成功したステップとして
 * 残る。停止が届いたとき発行前だった呼び出しは、失敗したステップとして残る。どれも
 * 走らなかったことに変わりはないので、状態語より先に「未実行」を出す。行を眺めるだけの
 * 人にも分かるよう、失敗の色は使わない。描く準備を待っているページも失敗ではなく、行の文が
 * そう言うので状態語は出さない。
 */
function toolRowStatus(entry: ToolEntry, copy: Copy): ToolRowStatus | null {
  if (entry.outcome === 'not_executed') return { label: copy.toolNotRun, failed: false };
  if (entry.status !== 'success') {
    return { label: copy.toolStatus[entry.status], failed: entry.status !== 'processing' };
  }
  return entry.outcome === 'completed' || entry.outcome === 'preparing'
    ? null
    : { label: copy.toolNotRun, failed: false };
}

/** 連続する同じツールをまとめた行の見出し。数え方は言語を問わない。 */
/** Screen-reader name for a collapsed run of one tool; the visible row shows only the name. */
function toolGroupAccessibleName(name: string, count: number): string {
  return `${name} ${count}`;
}

const COPY: Record<'en' | 'ja', Copy> = {
  en: {
    conversation: 'Action conversation',
    agentWork: "Pantaray's work",
    section: 'Section',
    step: 'Step',
    run: 'Run',
    assistant: 'Assistant',
    toolImages: {
      list: (count) => `${count} screenshot${count === 1 ? '' : 's'}`,
      imageAlt: (position, count) => `Screenshot ${position} of ${count}`,
      open: (position, count) => `Open screenshot ${position} of ${count}`,
      missing: 'Image unavailable',
      lightbox: {
        dialogLabel: 'Screenshot',
        close: 'Close',
        reveal: 'Show in Finder',
      },
    },
    finalAnswer: 'Final answer',
    failure: 'Action failed',
    canceled: 'Action canceled',
    runStatus: {
      running: 'Running',
      approval_pending: 'Waiting for approval',
    },
    toolStatus: {
      processing: 'Running',
      error: 'Failed',
      timeout: 'Timed out',
    },
    toolNotRun: 'Not run',
    toolResultUnavailable: 'Result unavailable',
    toolAwaitingApproval: 'Waiting for approval',
  },
  ja: {
    conversation: 'アクションの会話',
    agentWork: 'Pantarayの作業',
    section: '区間',
    step: 'ステップ',
    run: '実行',
    assistant: 'アシスタント',
    toolImages: {
      list: (count) => `スクリーンショット ${count} 件`,
      imageAlt: (position, count) => `スクリーンショット ${position} / ${count}`,
      open: (position, count) => `スクリーンショット ${position} / ${count} を開く`,
      missing: '画像を表示できません',
      lightbox: {
        dialogLabel: 'スクリーンショット',
        close: '閉じる',
        reveal: 'Finder で表示',
      },
    },
    finalAnswer: '最終回答',
    failure: 'アクションに失敗しました',
    canceled: 'アクションをキャンセルしました',
    runStatus: {
      running: '実行中',
      approval_pending: '確認待ち',
    },
    toolStatus: {
      processing: '実行中',
      error: '失敗',
      timeout: 'タイムアウト',
    },
    toolNotRun: '未実行',
    toolResultUnavailable: '結果未取得',
    toolAwaitingApproval: '承認待ち',
  },
};

export function StepItem({
  line,
  actionId,
  runLabel,
  announceToolStatus,
  runStopped,
  awaitingApproval = false,
  toolOutputLoader,
  onFocusedContentUnmount,
}: {
  line: ActionConversationRunLine;
  actionId: string | null;
  runLabel: string;
  announceToolStatus: boolean;
  runStopped: boolean;
  awaitingApproval?: boolean;
  toolOutputLoader: ActionToolOutputLoader;
  onFocusedContentUnmount: (key: string) => void;
}) {
  const { language } = useI18n();
  const copy = COPY[language];

  const contentRef = useRef<HTMLElement>(null);
  const contentKey = 'key' in line ? line.key : `outcome:${line.runId}`;
  const handleFocusedUnmount = useCallback(
    () => onFocusedContentUnmount(contentKey),
    [contentKey, onFocusedContentUnmount]
  );
  useLayoutEffect(
    () => () => {
      if (contentRef.current?.contains(document.activeElement)) handleFocusedUnmount();
    },
    [handleFocusedUnmount]
  );

  if (line.kind === 'user') return <UserItem item={line} />;
  if (line.kind === 'tool') {
    const entry = line.entry;
    const running = entry.status === 'processing';
    const resultUnavailable = running && runStopped;
    const display = resolveToolDisplay(entry.label, language);
    const readableOutput = actionId !== null && !running && entry.output_available;
    return (
      <ToolRow
        Icon={display.Icon}
        line={
          resultUnavailable
            ? { text: entry.subject ?? display.name, mono: false }
            : resolveToolLine(entry.label, language, {
                subject: entry.subject,
                running,
                outcome: entry.outcome,
              })
        }
        // 引数を持たないツールは、結果の頭を添えて何が返ったかだけでも見せる。
        preview={entry.subject === null ? entry.output_preview : null}
        stepNumber={entry.step_number}
        runLabel={runLabel}
        status={
          resultUnavailable
            ? { label: copy.toolResultUnavailable, failed: false }
            : running && awaitingApproval
              ? { label: copy.toolAwaitingApproval, failed: false }
              : toolRowStatus(entry, copy)
        }
        announceStatus={announceToolStatus}
        images={entry.images}
        imagesCopy={copy.toolImages}
        outputKey={readableOutput && actionId !== null ? { actionId, stepId: entry.step_id } : null}
        loader={toolOutputLoader}
        onFocusedContentUnmount={handleFocusedUnmount}
      />
    );
  }

  const title =
    line.kind === 'assistant'
      ? copy.assistant
      : line.kind === 'final_output'
        ? copy.finalAnswer
        : line.status === 'error'
          ? copy.failure
          : copy.canceled;
  return (
    <section
      ref={contentRef}
      className="action-conversation__outcome"
      aria-label={`${title}, ${runLabel}`}
    >
      {line.kind === 'assistant' || line.kind === 'final_output' ? (
        <MarkdownBlock text={line.text} isStreamFinished />
      ) : (
        <p>{line.text}</p>
      )}
      {line.kind === 'final_output' ? <CopyAnswerButton markdown={line.text} /> : null}
    </section>
  );
}

function ToolGroup({
  group,
  actionId,
  runLabel,
  toolOutputLoader,
  onFocusedContentUnmount,
}: {
  group: ToolGroupLine;
  actionId: string | null;
  runLabel: string;
  toolOutputLoader: ActionToolOutputLoader;
  onFocusedContentUnmount: (key: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const { language } = useI18n();
  const copy = COPY[language];
  const accessibleName = toolGroupAccessibleName(group.display.name, group.lines.length);
  const { Icon } = group.display;

  return (
    <div className="action-conversation__tool-group">
      <button
        className="action-conversation__disclosure action-conversation__tool-line"
        type="button"
        aria-label={`${accessibleName}, ${runLabel}, ${copy.step} ${group.lines[0].entry.step_number}`}
        aria-expanded={expanded}
        onClick={() => setExpanded(!expanded)}
      >
        <Icon className="action-conversation__tool-icon" size={14} aria-hidden />
        <span className="action-conversation__tool-text">{group.display.name}</span>
      </button>
      {expanded
        ? group.lines.map((line) => (
            <StepItem
              key={line.key}
              line={line}
              actionId={actionId}
              runLabel={runLabel}
              announceToolStatus={false}
              runStopped={true}
              toolOutputLoader={toolOutputLoader}
              onFocusedContentUnmount={onFocusedContentUnmount}
            />
          ))
        : null}
    </div>
  );
}

function WorkSection({
  section,
  position,
  actionId,
  runLabel,
  settled,
  expansionKey,
  liveToolKey,
  awaitingApproval,
  toolOutputLoader,
  onFocusedContentUnmount,
  registerDisclosure,
}: {
  section: AgentWorkSection;
  position: number;
  actionId: string | null;
  runLabel: string;
  settled: boolean;
  expansionKey: string;
  liveToolKey: string | undefined;
  awaitingApproval: boolean;
  toolOutputLoader: ActionToolOutputLoader;
  onFocusedContentUnmount: (key: string) => void;
  registerDisclosure: (key: string, element: HTMLButtonElement | null) => void;
}) {
  const { language } = useI18n();
  const copy = COPY[language];
  const rootRef = useRef<HTMLDivElement>(null);
  const [expansion, setExpansion] = useState({ key: expansionKey, expanded: false });
  const expanded = expansion.key === expansionKey && expansion.expanded;
  const count = section.lines.filter((line) => line.kind === 'tool').length;
  const disclosureRef = useCallback(
    (element: HTMLButtonElement | null) => registerDisclosure(section.key, element),
    [registerDisclosure, section.key]
  );
  useLayoutEffect(
    () => () => {
      if (rootRef.current?.contains(document.activeElement)) onFocusedContentUnmount(section.key);
    },
    [onFocusedContentUnmount, section.key]
  );
  return (
    <div ref={rootRef} className="action-conversation__work">
      <button
        ref={disclosureRef}
        className="action-conversation__disclosure"
        type="button"
        aria-label={`${copy.agentWork}${count ? ` ${count}` : ''}, ${runLabel}, ${copy.section} ${position}`}
        aria-expanded={expanded}
        onClick={() => setExpansion({ key: expansionKey, expanded: !expanded })}
      >
        {copy.agentWork}
        {count > 0 ? (
          <span className="action-conversation__count" aria-hidden>
            {count}
          </span>
        ) : null}
      </button>
      {expanded
        ? (settled ? groupRunLines(section.lines, language) : section.lines).map((line) =>
            line.kind === 'tool_group' ? (
              <ToolGroup
                key={line.key}
                group={line}
                actionId={actionId}
                runLabel={runLabel}
                toolOutputLoader={toolOutputLoader}
                onFocusedContentUnmount={onFocusedContentUnmount}
              />
            ) : (
              <StepItem
                key={'key' in line ? line.key : `outcome:${line.runId}`}
                line={line}
                actionId={actionId}
                runLabel={runLabel}
                announceToolStatus={line.kind === 'tool' && line.key === liveToolKey}
                runStopped={settled}
                awaitingApproval={awaitingApproval}
                toolOutputLoader={toolOutputLoader}
                onFocusedContentUnmount={onFocusedContentUnmount}
              />
            )
          )
        : null}
    </div>
  );
}

export function RunItem({
  run,
  actionId,
  isLatest,
  stopped,
  position,
  toolOutputLoader,
  onFocusedRunUnmount,
}: {
  run: ActionConversationRunItem;
  actionId: string | null;
  isLatest: boolean;
  stopped: boolean;
  position: number;
  toolOutputLoader: ActionToolOutputLoader;
  onFocusedRunUnmount: () => void;
}) {
  const { language, formatDateTime } = useI18n();
  const copy = COPY[language];
  const settled = stopped || (run.status !== 'running' && run.status !== 'approval_pending');
  const finalAnswerVisible = foldsCommentary(run.lines);
  const sections = groupAgentWork(run.lines);
  const expansionKey = `${settled}:${finalAnswerVisible}`;
  const runRef = useRef<HTMLElement>(null);
  const disclosures = useRef(new Map<string, HTMLButtonElement>());
  const focusKey = useRef<string | null>(null);
  const requestWorkFocus = useCallback((key: string) => {
    focusKey.current = key;
  }, []);
  const registerDisclosure = useCallback((key: string, element: HTMLButtonElement | null) => {
    if (element) disclosures.current.set(key, element);
    else disclosures.current.delete(key);
  }, []);
  const toolLines = run.lines.filter((line) => line.kind === 'tool');
  const liveToolKey = isLatest && !settled ? toolLines[toolLines.length - 1]?.key : undefined;
  // A run waiting for approval has paused its whole process, so none of its
  // processing Tools is actually running.
  const awaitingApproval = run.status === 'approval_pending' && !settled;
  const runLabel = `${copy.run} ${position}: ${formatDateTime(new Date(run.startedAt))}`;
  useLayoutEffect(() => {
    if (focusKey.current === null) return;
    const section = sections.find(
      (item) =>
        item.kind === 'agent_work' && item.lines.some((line) => line.key === focusKey.current)
    );
    focusKey.current = null;
    const disclosure = section && 'key' in section ? disclosures.current.get(section.key) : null;
    if (disclosure) disclosure.focus();
    else onFocusedRunUnmount();
  });
  useLayoutEffect(
    () => () => {
      if (runRef.current?.contains(document.activeElement)) onFocusedRunUnmount();
    },
    [onFocusedRunUnmount]
  );
  let workPosition = 0;
  return (
    <article ref={runRef} className="action-conversation__run" aria-label={runLabel}>
      {sections.map((section) =>
        section.kind === 'agent_work' ? (
          <WorkSection
            key={`work:${section.key}`}
            section={section}
            position={++workPosition}
            actionId={actionId}
            runLabel={runLabel}
            settled={settled}
            expansionKey={expansionKey}
            liveToolKey={liveToolKey}
            awaitingApproval={awaitingApproval}
            toolOutputLoader={toolOutputLoader}
            onFocusedContentUnmount={requestWorkFocus}
            registerDisclosure={registerDisclosure}
          />
        ) : (
          <StepItem
            key={'key' in section ? section.key : `outcome:${section.runId}`}
            line={section}
            actionId={actionId}
            runLabel={runLabel}
            announceToolStatus={false}
            runStopped={settled}
            awaitingApproval={awaitingApproval}
            toolOutputLoader={toolOutputLoader}
            onFocusedContentUnmount={requestWorkFocus}
          />
        )
      )}
    </article>
  );
}

export function ActionConversationView({
  view,
  lifecycle = null,
  toolOutputLoader,
}: {
  view: ActionConversationViewModel;
  lifecycle?: ActionLiveSnapshot['lifecycle'];
  toolOutputLoader: ActionToolOutputLoader;
}) {
  const { language } = useI18n();
  const copy = COPY[language];
  const conversationRef = useRef<HTMLElement>(null);
  const actionId = view.action?.action_id ?? null;
  const runs = view.items.filter((item): item is ActionConversationRunItem => item.kind === 'run');
  const restoreConversationFocus = useCallback(() => conversationRef.current?.focus(), []);
  const latestRun = runs.find((run) => run.runId === view.action?.latest_run_id) ?? null;
  // 実行が始まったことと、確認を待っていることは、ツール行が 1 つも出ていなくても伝わらないと
  // いけない。画面ではヘッダーが実行状態を示すので、ここは読み上げにだけ渡す。
  const announcedRunStatus = lifecycle
    ? lifecycle.status === 'processing'
      ? copy.runStatus.running
      : ''
    : latestRun !== null &&
        (latestRun.status === 'running' || latestRun.status === 'approval_pending')
      ? copy.runStatus[latestRun.status]
      : '';

  return (
    <section
      ref={conversationRef}
      tabIndex={-1}
      className="action-conversation"
      aria-label={copy.conversation}
    >
      <span className="action-conversation__sr-only" role="status" aria-live="polite">
        {announcedRunStatus}
      </span>
      <ol className="action-conversation__items">
        {view.items.map((item) => (
          <li key={item.kind === 'run' ? `run:${item.runId}` : item.key}>
            {item.kind === 'run' ? (
              <RunItem
                run={item}
                actionId={actionId}
                isLatest={item.runId === (lifecycle?.processId ?? view.action?.latest_run_id)}
                stopped={
                  lifecycle !== null &&
                  (lifecycle.status !== 'processing' || lifecycle.processId !== item.runId)
                }
                position={runs.indexOf(item) + 1}
                toolOutputLoader={toolOutputLoader}
                onFocusedRunUnmount={restoreConversationFocus}
              />
            ) : (
              <UserItem item={item} />
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}
