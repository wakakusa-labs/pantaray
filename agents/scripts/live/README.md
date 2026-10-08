# 実 API に当てる手動チェック

未ログインの利用者が自分の API キーで Pantaray を使えることを、**実際の OpenAI / Tavily API**
に当てて確認するスクリプト置き場。pytest の suite には入れない（CI から実 API を叩かないため）。
手で走らせて、`PASS` / `FAIL` の行と最後の `failures=N` を読む。失敗があれば終了コードは 1。

3 本ある。下にいくほど広い範囲を通す。

| スクリプト | 通る範囲 | 必要な鍵 |
|---|---|---|
| `direct_llm_smoke.py` | `LocalLlmProxyClient.generate_content` を direct 経路で実 OpenAI へ | `OPENAI_API_KEY` |
| `direct_web_tools_smoke.py` | `invoke_web_tools_wrapper` を実 Tavily へ | `TAVILY_API_KEY` |
| `direct_action_e2e.py` | 本物の helper プロセス + 制御ソケット + ローカル API / WS で Action を端から端まで | 両方 |
| `insight_replay.py` | 変更前のリビジョンとこのツリーで short Insight を同じ入力で実行して並べる | `OPENAI_API_KEY` |

## 鍵の渡し方

`OPENAI_API_KEY` と `TAVILY_API_KEY` を環境変数で渡す。自分で用意した開発用の鍵を使い、
**値を画面・ログ・commit に出さない**。ファイルに置いてあるなら、そのファイルを `cat` せず
コマンド置換でそのまま環境変数へ入れる（`set -x` もしない）。
スクリプトは例外を出すときに鍵を `<redacted>` へ置換する。

```sh
cd agents

OPENAI_API_KEY="$(your-secret-lookup openai)" \
TAVILY_API_KEY="$(your-secret-lookup tavily)" \
PYTHONPATH=src:packages/pantaray-llm/src .venv/bin/python scripts/live/direct_action_e2e.py
```

モデルは既定で `gpt-6-luna`。`SMOKE_MODEL` で変えられる。
`direct_action_e2e.py` の 1 回の通しで OpenAI は 40 回前後、Tavily は数回呼ぶ
（Action 本体に加えて、背景ジョブの活動要約 5 本と memory update が動くため）。

`direct_action_e2e.py --barrier` は、既定の 9 項目の代わりに**停止バリア**のシナリオを流す
（下の「`--barrier` が確認していること」）。所要 15 分前後、OpenAI は 70 回前後。
切り替え先のモデルは既定で `gpt-5.6-terra`、`SMOKE_ALTERNATE_MODEL` で変えられる。

## `insight_replay.py` で変更前後を比べる

1 回の実行で、`--before` のリビジョン（既定は `origin/develop` との merge-base）を一時 worktree に
取り出し、そこの `InsightAgent.generate` とこのツリーのものを同じ入力で `--runs` 回ずつ走らせ、
並べて表示してから worktree を消す。入力の既定は架空の 1 時間分の作業で、`--fixture` で同じ形の
JSON を渡せる。memory の検索先は空の新しい DB。モデルの既定は insight の purpose が本番で使うもの。

```sh
cd agents
OPENAI_API_KEY="$(your-secret-lookup openai)" PYTHONPATH=src:packages/pantaray-llm/src \
  .venv/bin/python scripts/live/insight_replay.py --runs 3
```

## `direct_action_e2e.py` が確認していること

`/tmp` 直下の使い捨てディレクトリ（制御ソケットの macOS パス長制限に収まるよう `/tmp` 直下）に
DB・artifact・ログ・ソケットを作り、Electron main と同じやり方で helper を起動する。
依頼者の実環境（`~/Library/Application Support/Pantaray`）には一切触らない。
成功したら隔離ディレクトリを消し、失敗したら調査用に残してパスを出す。

1. `helper_started` — `python -m pantaray_agents --host 127.0.0.1 --port 0` を起動し、
   制御ソケットの `status` が自分の `helper_instance_id` を返すまで待つ。未設定の初期状態
   （`configured=false` / route は `unconfigured` / cloud session は `absent`）を確認する。
2. `configure_direct` — cloud session なし・OpenAI の API キー・Tavily キーを `configure` で渡し、
   `llm_route` と `web_search_route` が `direct` になり、所有者が guest のローカル owner のままであること。
3. `action_with_web_search` — ローカル API の `POST /v1/agents/users/{owner}/actions/messages` で
   Action を投入し、renderer と同じく WS へ `resume_session` を送って live に繋ぎ、
   実 OpenAI → `web_search`（実 Tavily）→ 最終応答まで進んで `success` で終わること。
   WS に `process_started` / `action_step` / `process_completed` が流れること。
4. `history_page` — `GET /api/agent/history` にその Action が出て、
   `GET .../actions/{id}/state` で最終応答が読み直せること。
5. `background_activity_summary` — 何も仕込まずに、worker の定期スケジュールが入れた
   活動要約ジョブが claim されて実 OpenAI で完了すること（未ログイン + 保存した接続だけで
   背景ジョブが回ることの確認）。
6. `missing_connection_*` / `invalid_key_*` — `llm_connection` を消した場合と不正なキーの場合に、
   Action が黙って queued で止まらず利用者に見える `error` 終端へ到達すること
   （`*_reaches_terminal_error`）と、その終端が直し方を名指ししていること
   （`*_failure_is_actionable`: `ACTION_CONNECTION_NOT_CONFIGURED` /
   `ACTION_CONNECTION_REJECTED` と、汎用の「Action execution failed.」でない文面）。
   proxy の生のコードと `suggested_action` は診断用の durable payload に残り、公開面には出ない。

## `--barrier` が確認していること

実効 identity を変える制御ソケット操作は、旧 identity の処理を止めてから切り替える
（設計 6.2 / 6.3 / 7.3、受け入れ A09）。単体テストは 1 つのイベントループで
`dispatch_control_request` を呼ぶが、本番は制御ソケットが専用スレッドの専用ループ、
Action は worker のスレッド、WS は uvicorn のループにいる。ここはその 3 つを跨がせる。
同じ接続先でのモデル変更は identity を変えず、進行中の Action を止めない。

1. `background_job_requeues_on_credential_clear` — 起動時の活動要約ジョブが LLM を呼んでいる最中に
   接続を解除する → そのジョブが `queued` に戻り、接続の復元後に `completed` になること
   （publish の直前の `require_current_route_identity` が効いていること）。
2. `model_switch_keeps_running_action` — 実行中にモデルを変更しても Action が `success` まで
   進み、WS が閉じないこと。送信本文のモデル切替は `test_next_request_uses_the_new_model_on_the_same_account` で検証する。
3. `next_action_uses_the_new_connection` — その直後に始めた Action が新しい設定で `success` になること。
4. `same_identity_stops_nothing` — 実行中に同じ内容の `set_llm_connection` を再送しても
   identity が変わらないので何も止まらず、Action が `success` まで進むこと。
5. `credential_clear_cancels_running_action` — 実行中の Action のツール実行を 1 回見てから
   接続を解除する → 制御ソケットの応答が 5 秒以内／Action が `canceled` で終端
   （`error` ではない）／所有者は変わらないので WS は閉じないこと。
6. `owner_switch_closes_sockets` / `owner_switch_restores_logged_out_owner` —
   実行中に `set_cloud_session` で別アカウントに切り替える → 応答が 5 秒以内／旧所有者に
   束縛した WS がサーバー側から `1001 owner_changed` で閉じる／Action が `canceled`／
   `status` の所有者がアカウントに変わる／新しい所有者で WS の handshake が通る。
   そのあと `clear_cloud_session`（signed_out）で未ログインの owner に戻り、
   canceled の Action がその所有者として読み直せること。
7. `restart_does_not_cancel` / `restart_resumes_to_terminal` — Action の実行中に helper を
   SIGKILL → 同じ隔離ディレクトリで起動し直す → 同じ接続で `configure` を送る →
   再開待ちの Action が `canceled` にならず（起動復旧がジョブを再投入しただけの
   `processing` のまま）、再開されて終端まで進むこと。
