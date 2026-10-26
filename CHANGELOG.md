# Changelog

Each release's section becomes its notes on GitHub Releases. Write for people who use Pantaray,
in English and Japanese: one short sentence per change, saying what changed for them, not how.

## 0.6.1

- On first launch, a setup screen walks you through connecting AI, turning on recording, adding web
  search and connecting apps, each as a checklist.
- Settings now lists the macOS permissions recording needs, and marks Settings when one is missing;
  Allow asks macOS directly.
- Settings is ordered like setup: AI, recording, then apps.
- Pantaray no longer only suggests work: it can make the result, such as a draft or a sheet, and let
  you know. Open it from the suggestion, and a task you start from there picks up from it.
- When nothing was recorded since the last check, for example overnight, Pantaray no longer makes
  suggestions about nothing.
- Searching your memory is much faster.
- A task that has answered you now stops there, instead of running on until you stop it, and its
  answer is shown once.
- Fewer tasks stop with an error when the AI leaves out part of a step.
- A Composio key that no longer works is marked on its row in Settings, and the key setup now asks
  for the permissions Composio actually offers.
- Buttons now have a filled face, so they look like buttons.
- Removed certificate authorities that are no longer trusted.

---

- 初めて起動したとき、AI の接続・記録・Web 検索・アプリの接続を、チェックリストで順に設定できるようになりました。
- 記録に必要な macOS の許可を設定に一覧で出し、足りないものがあれば設定に印を付けるようにしました。「許可」を押すと、macOS に直接許可を求めます。
- 設定の並びを、初回の設定と同じく AI、記録、アプリの順にしました。
- 作業を提案するだけでなく、下書きや表などの成果物を先に作って知らせるようになりました。提案から開けて、そこから頼んだタスクはその成果物の続きから進めます。
- 夜のあいだなど、前回から何も記録されていないときは、中身のない提案を出さないようにしました。
- 記憶の検索がずっと速くなりました。
- 返事をしたタスクがそこで止まるようになりました。止めるまで動き続けることはなくなり、返事も 1 回だけ表示します。
- AI が手順の一部を書き漏らしても、タスクがエラーで止まりにくくなりました。
- 使えなくなった Composio のキーを設定の行に表示するようにしました。キーの設定で求める権限も、Composio に実際にあるものに直しました。
- ボタンを塗りつぶした見た目にして、ボタンとわかるようにしました。
- 信頼できなくなった認証局を外しました。

## 0.6.0

- Connect Gmail, Google Calendar, Google Drive, Slack, Outlook, Teams, OneDrive, SharePoint, Box,
  Notion, Linear, GitHub, Supabase, YouTube and LinkedIn in Settings with a free Composio API key,
  including several accounts of one service.
- Choose for each account whether Pantaray may not use it, only read it, or also write to it; a new
  account starts as read only.
- Tasks can read your connected apps, such as your unread mail or a Slack channel, and write to them
  where you allow it; every write asks you first, showing the service, account, action and details.
- Suggestions can read your connected apps too, and see what arrived in them since the last check.
- Links to a Gmail message open it in the account it belongs to.
- In Settings, the AI connection method is chosen from a menu.
- After you stop a task right after sending a message, you can still send it more messages.
- A command that runs outside the sandbox now uses your real home folder, so apps such as Chrome no
  longer ask to reset your keychain.
- Commands that use the temporary folder without quoting its path no longer fail.
- A command can no longer reach Pantaray's private data by renaming a folder above it.
- Suggestions now fit every kind of work and life, not only software, and the "external change"
  kind no longer keeps raising security advisories.

---

- 設定から、無料の Composio の API キーで Gmail、Google カレンダー、Google ドライブ、Slack、Outlook、Teams、OneDrive、SharePoint、Box、Notion、Linear、GitHub、Supabase、YouTube、LinkedIn をつなげるようになりました。1 つのサービスで複数のアカウントもつなげます。
- アカウントごとに「使わない」「読み取りのみ」「読み書き」を選べます。つないだ直後は「読み取りのみ」です。
- タスクが、未読メールや Slack のチャンネルなど、つないだアプリの中身を読めるようになりました。許可したアカウントには書き込みもでき、書き込む前には毎回、サービス・アカウント・操作・内容を見せて確認します。
- 提案も、つないだアプリを読めるようになり、前回の確認から届いたものを見られます。
- Gmail のメールへのリンクが、そのメールのアカウントで開くようになりました。
- 設定の AI 接続の方法を、メニューから選ぶようになりました。
- メッセージを送ってすぐにタスクを止めたあとでも、続けてメッセージを送れるようになりました。
- サンドボックスの外で動くコマンドが本来のホームフォルダを使うようになり、Chrome などがキーチェーンのリセットを求めることがなくなりました。
- 一時フォルダのパスを引用符で囲まずに使うコマンドが、失敗しなくなりました。
- コマンドが上の階層のフォルダの名前を変えて、Pantaray の非公開のデータに届くことがないようにしました。
- 提案が、ソフトウェア開発に限らずどんな仕事や暮らしにも合うようになり、「外部の変化」の提案がセキュリティ情報ばかりになることもなくなりました。

## 0.5.4

- When a task is working and your message is waiting, press ↑ under it to send it now: the task
  stops what it is doing and carries on with your message right away.
- The copy button for a message you sent now sits under its bubble, so it no longer covers the text.
- In Settings, the web search key field links to app.tavily.com, where you can create a key.

---

- タスクが作業中で送ったメッセージが待っているとき、その下の ↑ を押すと今すぐ送れるようになりました。実行中の作業を止めて、すぐにそのメッセージの続きに進みます。
- 送ったメッセージのコピーボタンが吹き出しの下に出るようになり、文字に重ならなくなりました。
- 設定の Web 検索キーの欄から、キーを作成できる app.tavily.com を開けるようになりました。

## 0.5.3

- Tasks run again when you use ChatGPT with gpt-6.1-sol, gpt-6-luna or gpt-6-astra. Before, they
  failed on every message.
- Hover a message you sent in a task and press the button at its bottom right to copy it.

---

- ChatGPT の gpt-6.1-sol、gpt-6-luna、gpt-6-astra を選んでいるとき、タスクがどのメッセージでも失敗していた問題を直しました。
- タスクで自分が送ったメッセージにカーソルを合わせ、右下のボタンを押すとコピーできるようになりました。

## 0.5.2

- A PDF a task names now shows in the preview beside the conversation, and opening it no longer
  closes the task.
- Scrollbars now match the dark window.
- Tasks see more of a long result at once and read on through long files, command output and
  search results more reliably.
- File search and listing leave out what your .gitignore excludes, such as dependencies and
  build output.
- Suggestions can read all of a long command output.
- Subagents run on the model you chose, and tasks hand work to them only when it is large enough to
  be worth it, so answers come sooner and use fewer tokens.
- When a git checkout a task works in is behind its remote, the task is told, so it does not take
  old code for the current one.

---

- タスクが示した PDF が会話の横のプレビューに表示されるようになり、開いてもタスクが閉じなくなりました。
- スクロールバーが暗い画面に合う色になりました。
- タスクが長い結果を一度に多く見られるようになり、長いファイルやコマンドの出力、検索結果を最後まで読み進めやすくなりました。
- ファイルの検索や一覧で、.gitignore で除外した依存やビルドの出力などを含めないようになりました。
- 提案で、長いコマンドの出力を最後まで読めるようになりました。
- サブエージェントはあなたが選んだモデルで動き、任せる価値のある大きさの作業だけを任せるようになったため、回答が速くなり、トークンも減りました。
- タスクが作業する git のチェックアウトがリモートより遅れているときは、それを伝えるので、古いコードを現状と取り違えなくなりました。

## 0.5.1

- Activity recording now also reads windows whose text could not be read before. It needs the
  Screen Recording permission; grant it when Pantaray asks.
- When the AI declines to answer, Pantaray now says so instead of failing without a reason.
- With Claude models, the notes written between steps now show as a task's progress.
- Pantaray's memory keeps where things stand rather than a log of what happened, and condenses a
  memory file once it grows long.
- Tasks and suggestions now start from a short overview of you and your work, and can open your
  memory files whole when they need more.
- Memory search finds things by meaning as well as by words, and answers about twice as fast.
- Suggestions no longer offer again what you passed on unless something has changed.
- In the chat, Pantaray finds the suggestion you mention and starts it when you ask.
- Without a Pantaray sign-in or a web search key, tasks and suggestions can still read web pages,
  fetched from your Mac.
- Web search can look up several things in one go.
- Tasks read long command output and long web pages all the way through, not just the beginning.
- A new ChatGPT or OpenAI connection starts on GPT-6.1 Sol, and an Anthropic connection on Claude
  Sonnet 5.5.
- Fixed a task stalling after it rewrote a file it had read.
- Fixed keystrokes in a small window going to another app while another small window closed.
- Fixed the live work label running past the conversation column.
- Fixed a memory update that could lose a change when it rewrote a line with a link.

---

- 記録で、これまで文字を読み取れなかったウィンドウも読めるようになりました。画面収録の許可が必要です。Pantaray に求められたら許可してください。
- AI が回答を断ったときは、理由がないまま失敗するのではなく、そう伝えるようになりました。
- Claude のモデルでは、手順の合間に書かれるメモが作業の進み具合として表示されるようになりました。
- Pantaray の記憶は、出来事の記録ではなく「いまどうなっているか」を残すようになり、長くなった記憶のファイルはまとめ直すようになりました。
- 作業と提案は、あなたとあなたの仕事の短い概要から始め、必要なときは記憶のファイルを丸ごと読めるようになりました。
- 記憶の検索が、言葉だけでなく意味でも探せるようになり、約 2 倍速くなりました。
- 提案は、見送ったものを、状況が変わらない限りもう一度出さないようになりました。
- チャットで提案に触れると、Pantaray がその提案を探し、頼めば始めるようになりました。
- Pantaray へのログインや Web 検索のキーがなくても、作業と提案が Web ページを読めるようになりました。ページはこの Mac から取りに行きます。
- Web 検索で、いくつかのことを一度に調べられるようになりました。
- 作業が、長いコマンドの出力や長い Web ページを、最初だけでなく最後まで読めるようになりました。
- 新しく ChatGPT や OpenAI に接続すると GPT-6.1 Sol、Anthropic に接続すると Claude Sonnet 5.5 から始まるようになりました。
- 読み込んだファイルを作業が書き換えると、その作業が止まってしまう問題を直しました。
- 小さい画面を閉じている間、別の小さい画面に打った文字がほかのアプリに入ってしまう問題を直しました。
- 作業中の表示が、会話の幅からはみ出す問題を直しました。
- 記憶の更新で、リンクを含む行を書き直したときに変更が失われることがある問題を直しました。

## 0.5.0

- History is now Home: a sidebar lists the chat, your projects and your tasks, and the one you
  pick opens beside it.
- Hide or show the sidebar with the button at the top of the window, or with ⌘B.
- Two buttons next to Tasks start a new task in the main window or in a small window.
- Move a task or suggestion between the two windows yourself: "Open small" in the main window,
  "Open in main window" in the small one. Nothing moves on its own.
- An unsent message stays when you switch to something else, and a task's small window and the
  main window share the same draft.
- Manage projects in the sidebar: name a new one with ＋ and then add its folders, and rename it,
  set its organization or delete it from its … menu.
- Expand a project to see its folders, open each one in Finder or remove it, and drag projects to
  reorder them.
- The Workspace page is gone. A folder that was in no project now has a project of its own, named
  after the folder.
- "Copy link" copies a task's or suggestion's link. Paste it into a message and it becomes a chip
  with the item's title, so Pantaray knows which one you mean; "Show as text" turns it back.
- Click a task or suggestion named in a sent message to open it. A Pantaray link opened from
  another app goes straight to that item in the main window.
- Select text in a conversation and press "Add to reply" to quote it, with a comment if you like.
  A message can carry several quotes.
- File edits show as a diff, with the lines added and removed.
- Files a task creates or links appear under its answer. Open one to preview it beside the
  conversation: Markdown, text, images, PDF, HTML, Word documents and patch files show there, and
  the Open menu has Show in Finder, Quick Look, Open in default app, Open with… and Copy path.
- Stop now also stops the work Pantaray split off right away, and shows "Stopping…" until the
  task has stopped. If a stop fails, you are told and can try again.
- When you continue a stopped task, Pantaray can pick up the split-off work the stop ended.
- Messages a task never got to before you stopped it now run with your next message, and say
  "Runs with your next message".
- Withdraw a message that has not run yet with the ✕ under it.
- While a task works, its folded "Pantaray's work" line shows what it is doing right now.
- The Accept and Dismiss suggestion buttons now sit where the send button is. Words you write
  before dismissing are kept as your reason and start no task, and the small window closes.
- A task started from a suggestion shows the suggestion first, marked Approved, in the main window
  too.
- Suggestions vary more, learn from the reasons you give when you dismiss one, and no longer treat
  a mail or chat message as unanswered just because Pantaray did not see the reply.
- Until you choose otherwise, Pantaray edits files and runs commands in your workspace without
  asking. Work outside your workspace or outside the protected environment, and screen capture,
  still ask. To be asked every time, choose Ask under File edits and commands in Settings.
- Settings lists every model you can choose for your AI connection, and with OpenAI you can also
  enter another model name.
- The main window opens larger, and its conversations use the small window's larger text.
- Sidebar rows show only a white dot when something needs you: an approval, a suggestion waiting
  for your answer, or a finished task you have not looked at yet.
- The task list loads more as you scroll.
- Home goes back to what you had open before you went to Settings.
- Icon buttons show their names sooner when you point at them.
- A single line break in an answer now shows as a line break.
- The button that showed a task in the chat is gone; open the task in the main window instead.
- If a newer release comes out while an update is waiting, Pantaray gets the newer one, so one
  restart brings you up to date.
- Includes security updates.
- Fixed Pantaray being unable to start after it quit while a task was continuing after an
  approval.
- Fixed the chat getting only the beginning of a long answer from a task.
- Fixed chat replies sometimes quoting "An earlier message" that could not be shown.
- Fixed a suggestion's card in the chat saying "Waiting for approval" instead of "Suggestion".
- Fixed the note on a stopped or failed task not following the app's language.
- Fixed a small window opened from the main window appearing folded for a moment.
- Fixed a link in an answer sometimes replacing the app's page. Web links open in your browser.
- Fixed pasting text copied from Quick Look attaching a file instead.
- Fixed deleting a task removing an image the chat still shows.

---

- 「履歴」は「ホーム」になりました。サイドバーにチャット・プロジェクト・作業が並び、選んだものが隣に開きます。
- サイドバーは、ウィンドウ上部のボタンか ⌘B で隠したり出したりできます。
- 「作業」の横の 2 つのボタンで、新しい作業をメイン画面で始めるか、小さい画面で始めるかを選べるようになりました。
- 作業や提案を、メイン画面では「小さく開く」、小さい画面では「メイン画面で開く」で行き来できるようになりました。頼まない限り、勝手に移りません。
- 送る前の文章は、ほかの項目に切り替えても残ります。同じ作業の小さい画面とメイン画面では、書きかけの文章が共有されます。
- サイドバーでプロジェクトを管理できるようになりました。＋で名前を付けて作り、あとからフォルダを足します。名前の変更・組織の設定・削除は「…」のメニューからできます。
- プロジェクトを開くとフォルダが並び、フォルダごとに Finder で開いたり外したりできます。プロジェクトはドラッグで並べ替えられます。
- 「ワークスペース」の画面をなくしました。どのプロジェクトにも入っていなかったフォルダは、そのフォルダの名前で自分のプロジェクトを持つようになりました。
- 「リンクをコピー」で、作業や提案のリンクをコピーできるようになりました。メッセージに貼ると題名で表示され、どれのことか Pantaray に伝わります。「テキストに戻す」でリンクに戻せます。
- 送ったメッセージに入っている作業や提案を押すと、それが開くようになりました。ほかのアプリで Pantaray のリンクを開くと、メイン画面でその項目が開きます。
- 会話の文章を選んで「返信に追加」を押すと、引用して返信できるようになりました。ひと言添えることもでき、1 つのメッセージに何か所でも引用できます。
- ファイルの編集が、足した行と消した行の差分で表示されるようになりました。
- 作業が作ったファイルや回答で示したファイルが、回答の下に並ぶようになりました。押すと会話の隣にプレビューが開き、Markdown・テキスト・画像・PDF・HTML・Word の文書・パッチファイルをそこで見られます。「開く」のメニューから、Finder で表示・Quick Look で見る・既定のアプリで開く・アプリを選んで開く・パスをコピーもできます。
- 停止を押すと、Pantaray が手分けして進めていた作業もすぐに止まり、止まるまで「停止しています…」と出るようになりました。止められなかったときはそう伝え、もう一度押せます。
- 停止した作業を続けるとき、手分けして進めていた作業も Pantaray が引き継げるようになりました。
- 停止する前に作業が手を付けていなかったメッセージは、次に送るメッセージと一緒に実行されるようになり、「次の送信で実行」と表示されます。
- まだ実行されていないメッセージは、下の ✕ で取り消せるようになりました。
- 作業中は、たたんだ「Pantarayの作業」の行に、いま何をしているかが出るようになりました。
- 「承認」と「見送る」を、送信ボタンの位置に置きました。「見送る」の前に書いた文章は見送る理由として残り、作業は始まらず、小さい画面は閉じます。
- 提案から始めた作業は、メイン画面でも最初にその提案を「承認済み」の印付きで表示するようになりました。
- 提案の幅が広がり、見送るときに書いた理由から学ぶようになりました。また、メールやチャットの返信が見えないだけで、まだ返していないと決めつけなくなりました。
- 設定を選ぶまでは、ワークスペースの中でのファイルの編集とコマンドの実行を、確認せずに進めるようになりました。ワークスペースの外や安全な実行環境の外での作業、画面の取り込みは、これまでどおり確認します。毎回確認してほしいときは、設定の「ファイル編集・コマンド実行」で「確認」を選んでください。
- 設定で、AI 接続で選べるモデルがすべて一覧に出るようになりました。OpenAI では、ほかのモデル名も入力できます。
- メイン画面が大きめに開くようになり、会話の文字も小さい画面と同じ大きさになりました。
- サイドバーの各行は、確認が必要なときだけ白い点を出すようになりました。許可を待っているとき、提案が返事を待っているとき、終わった作業をまだ見ていないときです。
- 作業の一覧は、下までスクロールすると続きを読み込むようになりました。
- 「ホーム」を押すと、設定に移る前に開いていたものに戻るようになりました。
- アイコンのボタンに指していると、名前が早く出るようになりました。
- 回答の中の 1 つだけの改行も、そのまま改行として表示するようになりました。
- 作業をチャットで表示するボタンをなくしました。メイン画面で作業を開いてください。
- 更新が入るのを待っている間に新しい版が出ると、新しいほうを取ってくるようになりました。1 回の再起動で最新になります。
- セキュリティの更新を取り込みました。
- 承認のあとで続きを進めている作業の途中でアプリが終了すると、Pantaray が起動できなくなる問題を直しました。
- 作業の長い回答が、チャットには最初の部分しか届かない問題を直しました。
- チャットの返信が、表示できない「以前のメッセージ」を引用することがある問題を直しました。
- チャットの提案のカードに、「提案」ではなく「確認待ち」と出る問題を直しました。
- 停止や失敗した作業に出る文が、アプリの言語に合っていなかった問題を直しました。
- メイン画面から小さい画面を開くと、一瞬たたまれた形で出る問題を直しました。
- 回答の中のリンクを押すと、アプリの画面が置き換わることがある問題を直しました。Web のリンクはブラウザで開きます。
- Quick Look からコピーした文章を貼ると、ファイルが添付されてしまう問題を直しました。
- 作業を削除すると、チャットにまだ表示している画像まで消えてしまう問題を直しました。

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
