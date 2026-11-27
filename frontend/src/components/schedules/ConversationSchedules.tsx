import { CalendarClock } from 'lucide-react';
import { useId, useState } from 'react';

import { useI18n } from '@/context/useI18n';

import type { HookView } from '../../../electron/src/settings/hooksFetch';
import { ScheduleSwitch } from './ScheduleSwitch';
import {
  describeTrigger,
  formatInstant,
  hookTrigger,
  isNotWatching,
  isRunning,
} from './scheduleText';
import { useSchedules } from './useSchedules';
import './schedules.css';

/**
 * The schedules that belong to a conversation, above its composer: those that continue it, and
 * those asked for here that start a new conversation each time, since they have no other home.
 * Each says when it runs, and can be switched off and on, or run again once stopped.
 */
export function ConversationSchedules({ actionId }: { actionId: string }) {
  const { t } = useI18n();
  const { list, setEnabled } = useSchedules();
  const hooks =
    list?.hooks.filter(
      (hook) => (hook.target_action_id ?? hook.created_from_action_id) === actionId
    ) ?? [];
  if (hooks.length === 0) return null;
  return (
    <ul className="conversation-schedules" aria-label={t('schedules.strip.label')}>
      {hooks.map((hook) => (
        <ScheduleLine
          key={hook.hook_id}
          hook={hook}
          recordingReady={list?.recording_ready ?? false}
          setEnabled={setEnabled}
        />
      ))}
    </ul>
  );
}

function ScheduleLine({
  hook,
  recordingReady,
  setEnabled,
}: {
  hook: HookView;
  recordingReady: boolean;
  setEnabled: (hookId: string, enabled: boolean) => Promise<boolean>;
}) {
  const { t, language } = useI18n();
  const textId = useId();
  const [pending, setPending] = useState(false);
  const [failed, setFailed] = useState(false);
  const change = async (enabled: boolean) => {
    setPending(true);
    setFailed(false);
    const changed = await setEnabled(hook.hook_id, enabled);
    setPending(false);
    setFailed(!changed);
  };

  const when = describeTrigger(hookTrigger(hook), t, language);
  const newSession = hook.target_action_id === null;
  const stopped = hook.stopped_reason;
  let text: string;
  let note: string | null = null;
  if (stopped) {
    text = t(
      stopped === 'turn_failed' ? 'schedules.strip.stoppedTurn' : 'schedules.strip.stoppedStart',
      { when }
    );
    if (stopped === 'turn_failed')
      note = t(newSession ? 'schedules.strip.failedThere' : 'schedules.strip.failedHere');
  } else {
    const sentence = t('schedules.strip.line', {
      when,
      destination: t(
        newSession
          ? 'overlay.approvalRequired.hook.newSession'
          : 'overlay.approvalRequired.hook.thisSession'
      ),
    });
    text =
      isRunning(hook) && hook.next_fire_at
        ? t('schedules.strip.withNext', {
            sentence,
            time: formatInstant(hook.next_fire_at, language),
          })
        : sentence;
    if (isNotWatching(hook, recordingReady)) note = t('schedules.notWatching');
  }

  return (
    <li className="conversation-schedule">
      <CalendarClock
        size={16}
        strokeWidth={1.8}
        aria-hidden="true"
        className="conversation-schedule__icon"
      />
      <div className="conversation-schedule__copy">
        <p
          id={textId}
          className={
            stopped
              ? 'conversation-schedule__text conversation-schedule__text--turn'
              : hook.enabled
                ? 'conversation-schedule__text'
                : 'conversation-schedule__text conversation-schedule__text--off'
          }
        >
          {text}
        </p>
        {note ? <p className="conversation-schedule__note">{note}</p> : null}
        <details className="conversation-schedule__message">
          <summary>{t('overlay.approvalRequired.hook.message')}</summary>
          <p>{hook.message}</p>
        </details>
        {failed ? (
          <p className="conversation-schedule__note settings-section-error" role="alert">
            {t('schedules.changeFailed')}
          </p>
        ) : null}
      </div>
      {stopped ? (
        <button
          type="button"
          className="history-filter-button todo-import__register"
          aria-describedby={textId}
          disabled={pending}
          onClick={() => void change(true)}
        >
          {t('schedules.resume')}
        </button>
      ) : (
        <ScheduleSwitch
          on={hook.enabled}
          describedBy={textId}
          disabled={pending}
          onChange={(on) => void change(on)}
        />
      )}
    </li>
  );
}
