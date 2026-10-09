# Changelog

Each release's section becomes its notes on GitHub Releases. Write for people who use Pantaray,
in English and Japanese: one short sentence per change, saying what changed for them, not how.

## 0.4.0

- You can now chat with Pantaray: one ongoing conversation where you ask questions and hand over
  tasks. History opens on the chat; switch to your tasks at the top.
- In the chat, send text with images and files, and name a workspace project with @.
- Quote an earlier message to reply to it, and click a quote to jump to that message and back.
- The chat shows when Pantaray is typing, and a button to try again when it could not reply.
- When Pantaray replies while you are not looking at the chat, History and the chat switch show
  it as unread.
- A task the chat starts shows as a card you can open from the chat.
- The chat tells you when one of its tasks is waiting for you or has finished.
- The chat can look things up in your files, memory, recent activity, your tasks and the web
  before it answers.
- Ask the chat to take on a suggestion, for example "go ahead with that suggestion", and Pantaray
  starts it.
- Suggestions you have not answered yet are marked "Suggestion" in your task list.
- Pantaray also remembers what you tell it in the chat.
- "New conversation" is now "New task".
- A task window has a button that shows the task in the chat, and a light sweeps across the work
  while it runs.
- In Settings, choose where each kind of task window opens. By default, new tasks open at the
  center, and suggestions and windows opened from History at the top right.
- Task windows have a darker navy background.
- Choosing a model in Settings saves it right away.
- Subagents keep their own notes and follow the AGENTS.md files of the folders they work in.
- Suggestions can look at images and PDF pages, and look into several things at once.
- Your AGENTS.md can now override how Pantaray works by default.
- Pantaray reads text files saved as Shift_JIS or UTF-16.
- With an Anthropic API key, long tasks cost less, and you can choose Claude Opus 5.5, Sonnet 5.5,
  Haiku 5.5 and Fable 5.1.
- Long tasks connected with ChatGPT respond faster.
- Searching memory is faster.
- Fixed copying from task windows: right-click selected text to copy it, and the answer's copy
  button now works while the window is collapsed or another app is in front.
- Fixed the main window needing a second click after a task window opened.
- Fixed History briefly showing "Syncing" when it opens.
- Fixed editing a file with Windows line endings changing every line of it.
- Fixed recording sometimes staying off after you resumed it.
- Fixed hiding a suggestion bringing the main window in front of the app you were using.
- Fixed a command sometimes never finishing when it created and deleted temporary files.
- Fixed the chat, Suggestions and subagents sometimes stopping with an error after reading many
  images.

---

- Pantaray とチャットできるようになりました。ひと続きの会話の中で、質問したり、作業を頼んだりできます。履歴を開くと最初にチャットが出て、上部の切り替えで作業の一覧に移れます。
- チャットで、文章に画像やファイルを添えて送ったり、@ でワークスペースのプロジェクトを指定したりできるようになりました。
- 前のメッセージを引用して返信でき、引用を押すとそのメッセージへ移動して、また戻れるようになりました。
- チャットで、Pantaray が入力中であることと、返信できなかったときにもう一度試すボタンが出るようになりました。
- チャットを見ていない間に Pantaray が返信すると、履歴とチャットの切り替えに未読として表示されるようになりました。
- チャットが始めた作業は、チャットの中にカードで表示され、そこから開けるようになりました。
- チャットが始めた作業があなたの確認を待っているときや終わったときに、チャットで知らせるようになりました。
- チャットが、ファイル・記憶・最近の作業・作業の一覧・Web を調べてから答えられるようになりました。
- 届いた提案を「さっきの提案お願い」などとチャットで頼むと、Pantaray が引き受けて始めるようになりました。
- まだ答えていない提案は、作業の一覧に「提案」の印が付くようになりました。
- チャットで伝えたことも、Pantaray が覚えるようになりました。
- 「新しい会話」を「新しい作業」に改めました。
- 作業のウィンドウに、その作業をチャットで表示するボタンができ、実行中は作業の表示に光が流れるようになりました。
- 設定で、作業のウィンドウが開く位置を種類ごとに選べるようになりました。最初は、新しい作業は画面の中央に、提案と履歴から開く作業は右上に出ます。
- 作業のウィンドウの背景を、濃い紺色にしました。
- 設定でモデルを選ぶと、すぐに保存されるようになりました。
- サブエージェントが自分用のメモを持ち、作業するフォルダの AGENTS.md に従うようになりました。
- 提案のための調べものが、画像や PDF のページを見たり、いくつかのことを同時に調べたりできるようになりました。
- 仕事の進め方について Pantaray が既定で持っている方針を、自分の AGENTS.md で上書きできるようになりました。
- Shift_JIS や UTF-16 で保存されたテキストファイルを読めるようになりました。
- Anthropic の API キーで使うとき、長い作業の費用が下がり、Claude Opus 5.5・Sonnet 5.5・Haiku 5.5・Fable 5.1 を選べるようになりました。
- ChatGPT で接続しているとき、長い作業の応答が速くなりました。
- 記憶の検索が速くなりました。
- 作業のウィンドウでコピーできないことがある問題を直しました。選んだ文字を右クリックでコピーでき、回答のコピーボタンはウィンドウを折りたたんでいても、別のアプリを使っていても効きます。
- 作業のウィンドウが開いたあと、メインのウィンドウが 2 回クリックしないと反応しない問題を直しました。
- 履歴を開くと、一瞬「同期中」と表示される問題を直しました。
- Windows 形式の改行のファイルを編集すると、すべての行が書き換わってしまう問題を直しました。
- 記録を再開しても、記録が止まったままになることがある問題を直しました。
- 提案を隠すと、使っていたアプリの手前にメインのウィンドウが出てくる問題を直しました。
- 一時ファイルを作っては消すコマンドが、終わらなくなることがある問題を直しました。
- チャット・提案・サブエージェントが、画像をたくさん読んだあとにエラーで止まることがある問題を直しました。

## 0.3.3

- Fixed Suggestions not appearing when connected with ChatGPT.
- A conversation you start by replying to a suggestion now keeps the suggestion as its title in History.
- Includes security updates.

---

- ChatGPT で接続していると、提案が表示されなくなっていた問題を直しました。
- 提案に返信して始めた会話は、履歴で提案の文面を題名として保つようになりました。
- セキュリティの更新を取り込みました。

## 0.3.2

- Fixed Pantaray being unable to start, with History stuck loading, when a saved tool result file was missing.
- Fixed the update button sometimes closing Pantaray without installing the update.

---

- ツールの結果を保存したファイルが無くなっていると、Pantaray が起動できず、履歴が読み込み中のままになる問題を直しました。
- 更新のボタンを押すと、更新されないままアプリが終了することがある問題を直しました。

## 0.3.1

- Fixed Pantaray sometimes being unable to start after it was closed unexpectedly or updated.
- Fixed subagents failing to start once memory had grown large.
- Removed the current route summary from AI connection settings; the settings below already show it.
- Updated urllib3 to 2.8.0 to fix security issues.

---

- 予期せず終了したあとや更新のあとに、Pantaray が起動できなくなることがある問題を直しました。
- 記憶が大きくなると、サブエージェントを起動できなくなる問題を直しました。
- AI 接続の設定から、現在の経路の表示をなくしました。下の設定で同じ内容を確認できます。
- urllib3 を 2.8.0 に更新し、セキュリティの問題を直しました。

## 0.3.0

- Attach PDF, Word, Excel, PowerPoint and notebook files to a message, and Pantaray reads them.
- Copy one answer, or the whole conversation, from the conversation window.
- The main window now has a navigation rail on the left, and History, Workspace and Settings have a
  new layout.
- History shows what a running conversation is doing right now, under its entry.
- Ask Pantaray to remember or forget something, and it does.
- Suggestions now bring ideas that move your work forward, not only next steps.
- Commands can ask to change files in a folder outside your workspace, after you allow it.
- When a command cannot work inside Pantaray's protected environment, Pantaray can ask to run that
  one command outside it.
- Commands can now use the tools you use in Terminal, and read as far as the read-access setting
  allows.
- Pantaray follows instructions you write in an AGENTS.md file.
- ChatGPT connections can now use gpt-6.1-sol.
- Screen capture now takes only the window of the app it names.
- Pantaray retries when the AI connection stalls or drops.
- Fixed approvals and progress sometimes not appearing in a conversation reopened from History.
- Fixed Option+Space not being accepted as a shortcut.

---

- PDF、Word、Excel、PowerPoint、ノートブックのファイルをメッセージに添付でき、Pantaray が読めるようになりました。
- 会話の画面から、回答 1 つ、または会話全体をコピーできるようになりました。
- メインのウィンドウの左にアイコンの列ができ、履歴・ワークスペース・設定の見た目が新しくなりました。
- 実行中の会話がいま何をしているかが、履歴の項目の下に出るようになりました。
- 覚えておいてほしいこと、忘れてほしいことを頼めるようになりました。
- 次の一歩だけでなく、作業を前に進めるアイデアも提案するようになりました。
- コマンドが、ワークスペースの外のフォルダの変更を、許可を得てから行えるようになりました。
- Pantaray の安全な実行環境の中では動かないコマンドは、その 1 回だけ外で動かしてよいか確認するようになりました。
- コマンドが、ターミナルで使っているツールを使い、読み取り範囲の設定どおりに読めるようになりました。
- AGENTS.md に書いた指示に従うようになりました。
- ChatGPT の接続で gpt-6.1-sol を使えるようになりました。
- 画面の取り込みは、名前を指定したアプリのウィンドウだけを撮るようになりました。
- AI との接続が止まったり切れたりしたとき、やり直すようになりました。
- 履歴から開き直した会話に、許可や進み具合が出ないことがある問題を直しました。
- Option＋Space をショートカットとして登録できなかった問題を直しました。

## 0.2.4

- Type @ in a message to pick a workspace project.
- You can now add a new organization from a project's organization picker.
- History now has one search bar that filters as you type, next to New conversation.
- Suggestions are less likely to bring up things that are already done.
- A ready update now shows as a small button at the bottom left.
- Fixed Add folder sometimes opening a closed conversation window.

---

- メッセージで @ を打つと、ワークスペースのプロジェクトを選べるようになりました。
- プロジェクトの組織を選ぶところから、新しい組織を追加できるようになりました。
- 履歴の上部を検索欄と「新しい会話」ボタンの 1 行にまとめ、打つそばから絞り込まれるようになりました。
- もう終わったことが提案されにくくなりました。
- 更新の準備ができると、左下に小さなボタンが出るようになりました。
- 「フォルダを追加」で、閉じた会話の画面が開くことがある問題を直しました。

## 0.2.3

- You can now delete a conversation from History.

---

- 履歴から会話を削除できるようになりました。

## 0.2.2

- The conversation no longer jumps to the bottom while you scroll up to read.
- When an update is ready, a notice appears at the bottom left of the main window.
- The recording introduction now says that work summaries stay on this Mac.

---

- 上にスクロールして読んでいる間、会話が最下部に引き戻されなくなりました。
- 更新の準備ができると、メインのウィンドウの左下にお知らせが出るようになりました。
- 記録を始める画面で、作業のまとめがこの Mac に残ることを説明するようにしました。

## 0.2.1

- Fixed the first start after installing, which could stay on a loading screen. Pantaray also
  starts faster.

---

- インストール直後の最初の起動で、読み込み中のまま進まないことがある問題を直しました。起動も速くなりました。

## 0.2.0

- The first public release of Pantaray.

---

- Pantaray の最初の公開版です。
