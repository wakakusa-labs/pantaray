import { describe, expect, it } from 'vitest';

import { ACTION_TOOL_DISPLAY_NAMES, resolveToolDisplay, resolveToolLine } from './toolDisplayName';

/*
 * ActionAgent の TOOL_REGISTRY（agents/src/pantaray_agents/agents/action_agent/tools/__init__.py）
 * に登録された tool id。Python の登録表は TypeScript から読めないため、ここに写して固定する。
 * ツールを追加・改名したらこの配列と ACTION_TOOL_DISPLAY_NAMES の両方を更新する。
 */
const REGISTERED_TOOL_IDS = [
  'thinking',
  'memory_search',
  'memory_sql',
  'get_memory_reference',
  'link_memory',
  'unlink_memory',
  'remember',
  'web_search',
  'web_extract',
  'web_crawl',
  'read',
  'render_pdf_page',
  'list',
  'glob',
  'grep',
  'apply_patch',
  'bash',
  'run_python',
  'capture_screen',
  'read_action_plan',
  'write_action_plan',
  'draft_final_answer',
  'submit_final_answer',
  'history_fetch',
  'zanei_timeline',
  'zanei_query',
  'spawn_subagent',
  'send_message_to_subagent',
  'wait_subagents',
  'cancel_subagent',
] as const;

describe('resolveToolDisplay', () => {
  it('names every registered tool in both languages', () => {
    expect(Object.keys(ACTION_TOOL_DISPLAY_NAMES).sort()).toEqual([...REGISTERED_TOOL_IDS].sort());
    for (const toolId of REGISTERED_TOOL_IDS) {
      const names = ACTION_TOOL_DISPLAY_NAMES[toolId];
      expect(names, toolId).toBeDefined();
      // 日本語 UI に英語名を混ぜない。生の tool id もそのままは出さない。
      expect(names!.ja, toolId).not.toMatch(/^[\x20-\x7e]*$/);
      expect(names!.ja, toolId).not.toBe(toolId);
      expect(names!.en.length, toolId).toBeGreaterThan(0);
      // 「何をしたか」の文は完了形と進行形を語形で言い分ける。コマンドそのものを見せる行
      // （テンプレートが {subject} だけ）は、文ではないので状態語の側だけが進行を示す。
      for (const [done, running] of [
        [names!.jaDone, names!.jaRunning],
        [names!.enDone, names!.enRunning],
      ]) {
        expect(done, toolId).not.toBe('');
        if (done !== '{subject}') expect(running, toolId).not.toBe(done);
      }
    }
  });

  it.each([
    // 読んだのは記録であって、内容を吟味したわけではない。英語も「確認」に留める。
    ['zanei_timeline', '最近の操作を確認', 'Check recent activity'],
    // live イベントは ToolDefinition.name を載せてくるので、同じツールに畳まれる必要がある。
    ['Recent Computer Activity', '最近の操作を確認', 'Check recent activity'],
    ['zanei_query', '操作の詳細を確認', 'Check activity details'],
    ['draft_final_answer', '回答を作成', 'Draft the answer'],
    ['capture_screen', 'ウィンドウを撮影', 'Capture a window'],
    ['bash', 'コマンドを実行', 'Run a command'],
    // PDF だけでなく Office 文書のページも描くので、名前は「PDF」に限らない。
    ['render_pdf_page', 'ページを見る', 'Look at pages'],
    ['Look At Document Pages', 'ページを見る', 'Look at pages'],
  ])('resolves %s to the same tool in both languages', (label, ja, en) => {
    expect(resolveToolDisplay(label, 'ja')).toEqual({
      key: expect.any(String),
      name: ja,
      Icon: expect.anything(),
    });
    expect(resolveToolDisplay(label, 'en').name).toBe(en);
    expect(resolveToolDisplay(label, 'ja').key).toBe(resolveToolDisplay(label, 'en').key);
  });

  it('shows a command with replacement tokens exactly as it was recorded', () => {
    // `$$` や `$&` は置換文字列としての意味を持つ。素の subject を差し込むと崩れて出る。
    const command = 'echo $$ && grep -n \'$&\' src | sed "s/$`/x/"';

    expect(
      resolveToolLine('bash', 'en', { subject: command, running: false, outcome: 'completed' }).text
    ).toBe(command);
    expect(
      resolveToolLine('read', 'ja', { subject: '$&.py', running: false, outcome: 'completed' }).text
    ).toBe('$&.py を読み取りました');
  });

  it('humanizes an unknown tool id instead of showing raw snake_case', () => {
    expect(resolveToolDisplay('brand_new_tool', 'ja').name).toBe('Brand new tool');
    expect(resolveToolDisplay('brand_new_tool', 'en').name).toBe('Brand new tool');
  });
});

describe('resolveToolLine', () => {
  it.each([
    ['read', 'tests/test_retrieval.py', 'tests/test_retrieval.py を読み取りました'],
    ['render_pdf_page', 'report.pdf', 'report.pdf のページを見ました'],
    ['web_search', '日本語検索', 'ウェブを検索しました 日本語検索'],
    ['grep', 'retrieval (src)', 'retrieval (src) を検索しました'],
    ['capture_screen', 'Google Chrome', 'Google Chrome を撮影しました'],
    // 引数を持たないツールは主語なしで完結する。
    ['zanei_timeline', null, '最近の操作を確認しました'],
    ['draft_final_answer', null, '回答を作成しました'],
    // 引数の残っていない古い行は動詞の文を作らず、ツール名だけを出す。
    ['read', null, 'ファイルを読む'],
  ])('says what %s did', (label, subject, expected) => {
    expect(resolveToolLine(label, 'ja', { subject, running: false, outcome: 'completed' })).toEqual(
      {
        text: expected,
        mono: false,
      }
    );
  });

  it('shows a command as itself, in monospace', () => {
    const command = "PYTHONPATH=src .venv/bin/python - <<'PY'…";

    expect(
      resolveToolLine('bash', 'ja', { subject: command, running: false, outcome: 'completed' })
    ).toEqual({
      text: command,
      mono: true,
    });
    expect(
      resolveToolLine('bash', 'en', { subject: command, running: true, outcome: 'completed' }).text
    ).toBe(command);
  });

  it('reads as in flight while the step is still running', () => {
    expect(
      resolveToolLine('read', 'ja', { subject: 'a.py', running: true, outcome: 'completed' }).text
    ).toBe('a.py を読み取っています');
    expect(
      resolveToolLine('read', 'en', { subject: 'a.py', running: true, outcome: 'completed' }).text
    ).toBe('Reading a.py');
  });

  it('humanizes an unknown tool id instead of showing raw snake_case', () => {
    expect(
      resolveToolLine('brand_new_tool', 'ja', {
        subject: null,
        running: false,
        outcome: 'completed',
      })
    ).toEqual({
      text: 'Brand new tool',
      mono: false,
    });
  });

  // 承認されなかった呼び出しは success として残る。ツールごとの「〜しました」を使うと、
  // 断ったはずの編集を「しました」と言ってしまう。
  it.each([
    ['ja', 'src/app.py', 'src/app.py の実行は許可されませんでした'],
    ['ja', null, '実行は許可されませんでした'],
    ['en', 'src/app.py', 'src/app.py was not allowed'],
    ['en', null, 'Not allowed'],
  ] as const)('says in %s that a denied call did not run', (language, subject, expected) => {
    expect(
      resolveToolLine('apply_patch', language, { subject, running: false, outcome: 'denied' })
    ).toEqual({ text: expected, mono: false });
  });

  it('says a denied command did not run instead of showing it as a run command', () => {
    // bash の完了文は subject そのもの（等幅）。断られた行までそれを使うと、実行の跡に見える。
    expect(
      resolveToolLine('bash', 'ja', { subject: 'rm -rf build', running: false, outcome: 'denied' })
    ).toEqual({ text: 'rm -rf build の実行は許可されませんでした', mono: false });
  });

  // 停止が届いたとき発行前だった呼び出しは error として残る。ツールごとの
  // 「〜しました」を使うと、走っていないコマンドを「実行しました」と言ってしまう。
  it.each([
    ['ja', 'rm -rf build', 'rm -rf build は停止したため実行しませんでした'],
    ['ja', null, '停止したため実行しませんでした'],
    ['en', 'rm -rf build', 'rm -rf build did not run because you stopped the action'],
    ['en', null, 'Did not run because you stopped the action'],
  ] as const)(
    'says in %s that a call stopped before it started did not run',
    (language, subject, expected) => {
      expect(
        resolveToolLine('bash', language, { subject, running: false, outcome: 'not_executed' })
      ).toEqual({ text: expected, mono: false });
    }
  );

  // 描く準備を待っているページは失敗でも「見ました」でもない。
  it.each([
    ['ja', 'slides.pptx を表示する準備をしています'],
    ['en', 'Preparing to show slides.pptx'],
  ] as const)('says in %s that the pages are not ready to show yet', (language, expected) => {
    expect(
      resolveToolLine('render_pdf_page', language, {
        subject: 'slides.pptx',
        running: false,
        outcome: 'preparing',
      })
    ).toEqual({ text: expected, mono: false });
  });

  it.each([
    ['ja', '記録がオフのため最近の操作を確認できませんでした'],
    ['en', 'Could not check recent activity because recording is off'],
  ] as const)(
    'says in %s that recording was off instead of claiming a read',
    (language, expected) => {
      expect(
        resolveToolLine('zanei_timeline', language, {
          subject: null,
          running: false,
          outcome: 'unavailable',
        })
      ).toEqual({ text: expected, mono: false });
    }
  );
});
