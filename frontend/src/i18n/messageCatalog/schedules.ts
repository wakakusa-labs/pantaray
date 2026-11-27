import { defineMessages } from './defineMessages';

/**
 * A conversation's schedules: whether each runs and what stopped it. When one runs is worded
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
  },
});
