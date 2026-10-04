/**
 * ツール行の表示カタログ。
 *
 * ActionAgent のツール定義は agents/src/pantaray_agents/agents/action_agent/tools/__init__.py の
 * TOOL_REGISTRY が正。ここはその tool id に対する UI 表示（アイコン・名前・1 行の文）だけを持つ。
 *
 * 会話ページ（durable）は entry.label に tool id をそのまま載せ、実行中の live イベントは
 * ToolDefinition.name（英語）を載せるため、同じツールが 2 通りの綴りで UI に届く。agentName は
 * その live 側の綴りを tool id へ戻すためのエイリアスで、表示には使わない。
 *
 * 1 行の文は「何をしたか」を言う。{subject} はバックエンドが tool_args から取り出した 1 つの
 * 引数（読んだファイル、検索語、コマンド）で、そこだけが行ごとに変わる。引数の残っていない
 * 古い行では動詞の文が作れないので、ツール名（ja / en）へ落とす。
 */
import type { ActionConversationToolItem } from '../../../electron/src/actions/actionConversationModel';

import {
  Brain,
  Camera,
  ClipboardList,
  Clock,
  FileSearch,
  FileText,
  Folder,
  Globe,
  History,
  MessageSquare,
  Pencil,
  Search,
  Terminal,
  Users,
  Wrench,
  type LucideIcon,
} from 'lucide-react';

type ToolDisplayNames = Readonly<{
  /** ToolDefinition.name。live イベントの label と突き合わせるためだけに持つ。 */
  agentName: string;
  icon: LucideIcon;
  ja: string;
  en: string;
  jaDone: string;
  jaRunning: string;
  enDone: string;
  enRunning: string;
  /** subject がコマンドそのもののツール。行を等幅で見せる。 */
  mono?: true;
}>;

const TOOL_DISPLAY_NAMES = {
  thinking: {
    agentName: 'Deep Thought',
    icon: Brain,
    ja: 'じっくり考える',
    en: 'Think it through',
    jaDone: 'じっくり考えました',
    jaRunning: 'じっくり考えています',
    enDone: 'Thought it through',
    enRunning: 'Thinking it through',
  },
  memory_search: {
    agentName: 'Memory Search',
    icon: Brain,
    ja: '記憶を検索',
    en: 'Search memory',
    jaDone: '記憶を検索しました {subject}',
    jaRunning: '記憶を検索しています {subject}',
    enDone: 'Searched memory for {subject}',
    enRunning: 'Searching memory for {subject}',
  },
  memory_sql: {
    agentName: 'Memory SQL',
    icon: Brain,
    ja: '記憶を絞り込む',
    en: 'Query memory',
    jaDone: '記憶を絞り込みました {subject}',
    jaRunning: '記憶を絞り込んでいます {subject}',
    enDone: 'Queried memory with {subject}',
    enRunning: 'Querying memory with {subject}',
  },
  get_memory_reference: {
    agentName: 'Get Memory Reference',
    icon: Brain,
    ja: '記憶の出典を確認',
    en: 'Look up a memory reference',
    jaDone: '{subject} の出典を確認しました',
    jaRunning: '{subject} の出典を確認しています',
    enDone: 'Looked up the source of {subject}',
    enRunning: 'Looking up the source of {subject}',
  },
  link_memory: {
    agentName: 'Link Memory',
    icon: Brain,
    ja: '記憶を紐付ける',
    en: 'Link a memory',
    jaDone: '{subject} に記憶を紐付けました',
    jaRunning: '{subject} に記憶を紐付けています',
    enDone: 'Linked a memory to {subject}',
    enRunning: 'Linking a memory to {subject}',
  },
  unlink_memory: {
    agentName: 'Unlink Memory',
    icon: Brain,
    ja: '記憶の紐付けを外す',
    en: 'Unlink a memory',
    jaDone: '{subject} の紐付けを外しました',
    jaRunning: '{subject} の紐付けを外しています',
    enDone: 'Unlinked {subject}',
    enRunning: 'Unlinking {subject}',
  },
  remember: {
    agentName: 'Remember',
    icon: Brain,
    // 忘れて・直しての依頼もメモとして残るので、「覚えました」とは言わない。
    ja: '記憶にメモ',
    en: 'Note in memory',
    jaDone: '記憶にメモしました {subject}',
    jaRunning: '記憶にメモしています {subject}',
    enDone: 'Noted in memory: {subject}',
    enRunning: 'Noting in memory: {subject}',
  },
  web_search: {
    agentName: 'Web Search',
    icon: Search,
    ja: 'ウェブを検索',
    en: 'Search the web',
    jaDone: 'ウェブを検索しました {subject}',
    jaRunning: 'ウェブを検索しています {subject}',
    enDone: 'Searched the web for {subject}',
    enRunning: 'Searching the web for {subject}',
  },
  web_extract: {
    agentName: 'Web Extract',
    icon: Globe,
    ja: 'ページを読む',
    en: 'Read a web page',
    jaDone: '{subject} を読みました',
    jaRunning: '{subject} を読んでいます',
    enDone: 'Read {subject}',
    enRunning: 'Reading {subject}',
  },
  web_crawl: {
    agentName: 'Web Crawl',
    icon: Globe,
    ja: 'サイトをたどる',
    en: 'Crawl a site',
    jaDone: '{subject} をたどりました',
    jaRunning: '{subject} をたどっています',
    enDone: 'Crawled {subject}',
    enRunning: 'Crawling {subject}',
  },
  read: {
    agentName: 'Read File',
    icon: FileText,
    ja: 'ファイルを読む',
    en: 'Read a file',
    jaDone: '{subject} を読み取りました',
    jaRunning: '{subject} を読み取っています',
    enDone: 'Read {subject}',
    enRunning: 'Reading {subject}',
  },
  render_pdf_page: {
    agentName: 'Look At Document Pages',
    icon: FileText,
    ja: 'ページを見る',
    en: 'Look at pages',
    jaDone: '{subject} のページを見ました',
    jaRunning: '{subject} のページを見ています',
    enDone: 'Looked at pages of {subject}',
    enRunning: 'Looking at pages of {subject}',
  },
  list: {
    agentName: 'List Local Directory',
    icon: Folder,
    ja: 'フォルダを見る',
    en: 'List a folder',
    jaDone: '{subject} の中を見ました',
    jaRunning: '{subject} の中を見ています',
    enDone: 'Listed {subject}',
    enRunning: 'Listing {subject}',
  },
  glob: {
    agentName: 'Glob Local Files',
    icon: FileSearch,
    ja: 'ファイルを探す',
    en: 'Find files',
    jaDone: '{subject} に合うファイルを探しました',
    jaRunning: '{subject} に合うファイルを探しています',
    enDone: 'Searched for files matching {subject}',
    enRunning: 'Finding files matching {subject}',
  },
  grep: {
    agentName: 'Grep Local Text',
    icon: Search,
    ja: 'ファイルの中を検索',
    en: 'Search inside files',
    jaDone: '{subject} を検索しました',
    jaRunning: '{subject} を検索しています',
    enDone: 'Searched files for {subject}',
    enRunning: 'Searching files for {subject}',
  },
  apply_patch: {
    agentName: 'Apply Patch',
    icon: Pencil,
    ja: 'ファイルを編集',
    en: 'Edit a file',
    jaDone: '{subject} を編集しました',
    jaRunning: '{subject} を編集しています',
    enDone: 'Edited {subject}',
    enRunning: 'Editing {subject}',
  },
  bash: {
    agentName: 'Bash',
    icon: Terminal,
    ja: 'コマンドを実行',
    en: 'Run a command',
    // コマンドそのものが「何をしたか」なので、動詞を足さずそのまま見せる。
    jaDone: '{subject}',
    jaRunning: '{subject}',
    enDone: '{subject}',
    enRunning: '{subject}',
    mono: true,
  },
  run_python: {
    agentName: 'Run Python',
    icon: Terminal,
    ja: 'Python を実行',
    en: 'Run Python',
    jaDone: '{subject}',
    jaRunning: '{subject}',
    enDone: '{subject}',
    enRunning: '{subject}',
    mono: true,
  },
  capture_screen: {
    agentName: 'Capture Screen',
    icon: Camera,
    ja: 'ウィンドウを撮影',
    en: 'Capture a window',
    jaDone: '{subject} を撮影しました',
    jaRunning: '{subject} を撮影しています',
    enDone: 'Captured {subject}',
    enRunning: 'Capturing {subject}',
  },
  read_action_plan: {
    agentName: 'Read Action Plan',
    icon: ClipboardList,
    ja: '計画を確認',
    en: 'Read the plan',
    jaDone: '計画を確認しました',
    jaRunning: '計画を確認しています',
    enDone: 'Read the plan',
    enRunning: 'Reading the plan',
  },
  write_action_plan: {
    agentName: 'Write Action Plan',
    icon: ClipboardList,
    ja: '計画を更新',
    en: 'Update the plan',
    jaDone: '計画を更新しました',
    jaRunning: '計画を更新しています',
    enDone: 'Updated the plan',
    enRunning: 'Updating the plan',
  },
  draft_final_answer: {
    agentName: 'Draft Final Answer',
    icon: MessageSquare,
    ja: '回答を作成',
    en: 'Draft the answer',
    jaDone: '回答を作成しました',
    jaRunning: '回答を作成しています',
    enDone: 'Drafted the answer',
    enRunning: 'Drafting the answer',
  },
  submit_final_answer: {
    agentName: 'Submit Final Answer',
    icon: MessageSquare,
    ja: '回答を送信',
    en: 'Send the answer',
    jaDone: '回答を送信しました',
    jaRunning: '回答を送信しています',
    enDone: 'Sent the answer',
    enRunning: 'Sending the answer',
  },
  history_fetch: {
    agentName: 'History Fetch',
    icon: History,
    ja: '履歴を確認',
    en: 'Look up history',
    jaDone: '履歴を確認しました {subject}',
    jaRunning: '履歴を確認しています {subject}',
    enDone: 'Looked up history {subject}',
    enRunning: 'Looking up history {subject}',
  },
  zanei_timeline: {
    agentName: 'Recent Computer Activity',
    icon: Clock,
    ja: '最近の操作を確認',
    // 読んだのは記録であって、内容を吟味したわけではない。英語も「確認した」に揃える。
    en: 'Check recent activity',
    jaDone: '最近の操作を確認しました',
    jaRunning: '最近の操作を確認しています',
    enDone: 'Checked recent activity',
    enRunning: 'Checking recent activity',
  },
  zanei_query: {
    agentName: 'Computer Activity Detail',
    icon: Clock,
    ja: '操作の詳細を確認',
    en: 'Check activity details',
    jaDone: '操作の詳細を確認しました {subject}',
    jaRunning: '操作の詳細を確認しています {subject}',
    enDone: 'Checked activity details {subject}',
    enRunning: 'Checking activity details {subject}',
  },
  spawn_subagent: {
    agentName: 'Spawn Subagent',
    icon: Users,
    ja: 'サブエージェントを起動',
    en: 'Start a subagent',
    jaDone: 'サブエージェントを起動しました {subject}',
    jaRunning: 'サブエージェントを起動しています {subject}',
    enDone: 'Started a subagent for {subject}',
    enRunning: 'Starting a subagent for {subject}',
  },
  send_message_to_subagent: {
    agentName: 'Send Message to Subagent',
    icon: Users,
    ja: 'サブエージェントへ指示',
    en: 'Message a subagent',
    jaDone: 'サブエージェントへ指示しました {subject}',
    jaRunning: 'サブエージェントへ指示しています {subject}',
    enDone: 'Messaged a subagent: {subject}',
    enRunning: 'Messaging a subagent: {subject}',
  },
  wait_subagents: {
    agentName: 'Wait for Subagents',
    icon: Users,
    ja: 'サブエージェントを待つ',
    en: 'Wait for subagents',
    jaDone: 'サブエージェントを待ちました {subject}',
    jaRunning: 'サブエージェントを待っています {subject}',
    enDone: 'Waited for {subject}',
    enRunning: 'Waiting for {subject}',
  },
  cancel_subagent: {
    agentName: 'Cancel Subagent',
    icon: Users,
    ja: 'サブエージェントを停止',
    en: 'Stop a subagent',
    // 停止は要求までしか言えない。安全な境界に達していない子は success のまま
    // `{status: "nonterminal"}` を返し、呼び出し側が改めて待つ。
    jaDone: '{subject} の停止を要求しました',
    jaRunning: '{subject} の停止を要求しています',
    enDone: 'Requested a stop for {subject}',
    enRunning: 'Requesting a stop for {subject}',
  },
} as const satisfies Readonly<Record<string, ToolDisplayNames>>;

export const ACTION_TOOL_DISPLAY_NAMES: Readonly<Record<string, ToolDisplayNames>> =
  TOOL_DISPLAY_NAMES;

// label は外部由来の文字列なので、素の添字ではなく Map で引く。
type CatalogEntry = readonly [toolId: string, names: ToolDisplayNames];
const ENTRIES: readonly CatalogEntry[] = Object.entries(TOOL_DISPLAY_NAMES);
const BY_TOOL_ID = new Map<string, CatalogEntry>(ENTRIES.map((entry) => [entry[0], entry]));
const BY_AGENT_NAME = new Map<string, CatalogEntry>(
  ENTRIES.map((entry) => [entry[1].agentName.toLowerCase(), entry])
);

export type ToolDisplay = Readonly<{
  /** 連続する同一ツールをまとめるためのキー。綴り違いを吸収した後の識別子。 */
  key: string;
  name: string;
  Icon: LucideIcon;
}>;

/** 行に出す 1 文。mono の行は subject がコマンドそのもの。 */
export type ToolLine = Readonly<{ text: string; mono: boolean }>;

type ToolOutcome = ActionConversationToolItem['entry']['outcome'];

const SUBJECT_SLOT = '{subject}';

/*
 * 呼ばれたことと、起きたことは別。承認されなかった呼び出しも、記録がオフのまま呼ばれた
 * 読み取りも、成功したステップとして残るので、ツールごとの「〜しました」では嘘になる。
 * どちらも起きなかったことは 1 通りしか言えないので、文はツールに依らず 1 つずつ持つ。
 */
const DENIED_LINES = {
  ja: { subject: '{subject} の実行は許可されませんでした', bare: '実行は許可されませんでした' },
  en: { subject: '{subject} was not allowed', bare: 'Not allowed' },
} as const;

const UNAVAILABLE_LINES = {
  ja: '記録がオフのため最近の操作を確認できませんでした',
  en: 'Could not check recent activity because recording is off',
} as const;

/** 停止が届いたとき、この呼び出しはまだ始まっていなかった。 */
const NOT_EXECUTED_LINES = {
  ja: {
    subject: '{subject} は停止したため実行しませんでした',
    bare: '停止したため実行しませんでした',
  },
  en: {
    subject: '{subject} did not run because you stopped the action',
    bare: 'Did not run because you stopped the action',
  },
} as const;

/** ページを描く準備がまだ済んでいない。失敗ではなく、準備が済めば見られる。 */
const PREPARING_LINES = {
  ja: '{subject} を表示する準備をしています',
  en: 'Preparing to show {subject}',
} as const;

/** 未登録のツールでも生の snake_case は出さず、読める語に均す。 */
function humanizeToolLabel(label: string): string {
  const spaced = label.replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

function lookup(label: string): CatalogEntry | undefined {
  const trimmed = label.trim();
  return BY_TOOL_ID.get(trimmed) ?? BY_AGENT_NAME.get(trimmed.toLowerCase());
}

export function resolveToolDisplay(label: string, language: 'en' | 'ja'): ToolDisplay {
  const entry = lookup(label);
  if (entry !== undefined) {
    return { key: entry[0], name: entry[1][language], Icon: entry[1].icon };
  }
  return {
    key: `label:${label.trim().toLowerCase()}`,
    name: humanizeToolLabel(label),
    Icon: Wrench,
  };
}

export function resolveToolLine(
  label: string,
  language: 'en' | 'ja',
  { subject, running, outcome }: { subject: string | null; running: boolean; outcome: ToolOutcome }
): ToolLine {
  if (outcome === 'unavailable') return { text: UNAVAILABLE_LINES[language], mono: false };
  if (outcome === 'denied' || outcome === 'not_executed') {
    const lines = outcome === 'denied' ? DENIED_LINES[language] : NOT_EXECUTED_LINES[language];
    if (subject === null) return { text: lines.bare, mono: false };
    // subject は記録されたままの文字列。置換文字列として解釈させない。
    return { text: lines.subject.replace(SUBJECT_SLOT, () => subject), mono: false };
  }
  if (outcome === 'preparing' && subject !== null) {
    return { text: PREPARING_LINES[language].replace(SUBJECT_SLOT, () => subject), mono: false };
  }
  const entry = lookup(label);
  if (entry === undefined) return { text: humanizeToolLabel(label), mono: false };
  const names = entry[1];
  const template =
    language === 'ja'
      ? running
        ? names.jaRunning
        : names.jaDone
      : running
        ? names.enRunning
        : names.enDone;
  if (!template.includes(SUBJECT_SLOT)) return { text: template, mono: false };
  if (subject === null) return { text: names[language], mono: false };
  // subject は記録されたままの文字列なので、置換文字列としては解釈させない。`$$` や `$&` を
  // 含むコマンドが、そのまま出さずに崩れてしまう。
  return { text: template.replace(SUBJECT_SLOT, () => subject), mono: names.mono === true };
}
