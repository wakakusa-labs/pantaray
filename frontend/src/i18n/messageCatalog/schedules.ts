import { defineMessages } from './defineMessages';

/**
 * A conversation's schedules and the list of them in Settings: whether each runs and what
 * stopped it. When one runs is worded
 * by `scheduleText.ts` with the approval panel's words (`overlay.approvalRequired.hook.*`).
 */
export const SCHEDULE_MESSAGES = defineMessages({
  en: {
    'schedules.notWatching': "Recording is off, so it isn't watching for now.",
    'schedules.resume': 'Resume',
    'schedules.switch': 'Run this schedule',
    'schedules.changeFailed': "Couldn't change it. Try again.",
    'schedules.strip.label': "This conversation's schedules",
    'schedules.strip.line': '{when}: {destination}.',
    'schedules.strip.withNext': '{sentence} Next: {time}.',
    'schedules.strip.stoppedTurn':
      'This schedule ({when}) failed several times in a row, so it stopped.',
    'schedules.strip.stoppedStart':
      "This schedule ({when}) couldn't start several times in a row, so it stopped.",
    'schedules.strip.failedHere': 'You can see how it failed in the conversation above.',
    'schedules.strip.failedThere': 'You can see how it failed in the conversations it started.',
    'schedules.next': 'Next: {time}',
    'schedules.askedIn': 'Asked in “{title}”',
    'schedules.askedInUntitled': 'Asked in a conversation',
    'schedules.stopped': 'Stopped',
    'schedules.stopped.turnFailed': 'It failed several times in a row, so it stopped.',
    'schedules.stopped.startFailed': "It couldn't start several times in a row, so it stopped.",
    'schedules.settings.title': 'Schedules',
    'schedules.settings.description': 'What you asked for in conversations, to run later.',
    'schedules.settings.loading': 'Loading…',
    'schedules.settings.loadFailed': "Couldn't load the schedules.",
    'schedules.settings.empty': 'No schedules yet.',
    'schedules.detail.next': 'Next',
    'schedules.detail.untilNone': 'Not set',
    'schedules.openConversation': 'Open the conversation',
  },
  ja: {
    'schedules.notWatching': '記録が止まっているので、いまは見ていません。',
    'schedules.resume': '再開する',
    'schedules.switch': 'この予定を動かす',
    'schedules.changeFailed': '変えられませんでした。もう一度お試しください。',
    'schedules.strip.label': 'この会話の予定',
    'schedules.strip.line': '{when}、{destination}。',
    'schedules.strip.withNext': '{sentence}次は {time} です。',
    'schedules.strip.stoppedTurn': '{when}の予定は、続けて失敗したので止めました。',
    'schedules.strip.stoppedStart': '{when}の予定は、続けて始められなかったので止めました。',
    'schedules.strip.failedHere': '失敗したときの様子は、上の会話で見られます。',
    'schedules.strip.failedThere': '失敗したときの様子は、そのとき始めた会話で見られます。',
    'schedules.next': '次は {time}',
    'schedules.askedIn': '「{title}」で頼みました',
    'schedules.askedInUntitled': '会話で頼みました',
    'schedules.stopped': '止まっています',
    'schedules.stopped.turnFailed': '続けて失敗したので止めました。',
    'schedules.stopped.startFailed': '続けて始められなかったので止めました。',
    'schedules.settings.title': '予定',
    'schedules.settings.description': '会話で頼んだ、あとで動く予定です。',
    'schedules.settings.loading': '読み込み中…',
    'schedules.settings.loadFailed': '予定を読み込めませんでした。',
    'schedules.settings.empty': 'まだ予定はありません。',
    'schedules.detail.next': '次',
    'schedules.detail.untilNone': '決めていません',
    'schedules.openConversation': '会話を開く',
  },
});
