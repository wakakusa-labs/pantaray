import { ChevronDown, ChevronRight } from 'lucide-react';
import { useId, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { ScheduleSwitch } from '@/components/schedules/ScheduleSwitch';
import {
  describeTrigger,
  formatInstant,
  hookTrigger,
  isNotWatching,
  isRunning,
} from '@/components/schedules/scheduleText';
import { useSchedules } from '@/components/schedules/useSchedules';
import { useI18n } from '@/context/useI18n';
import { historySelectionSearch } from '@/history/historySelection';

import type { HookView } from '../../../../electron/src/settings/hooksFetch';
import './schedulesSection.css';

// Stopped ones first, since they wait on the user; then those that run, then those switched off.
const rank = (hook: HookView) => (hook.stopped_reason ? 0 : hook.enabled ? 1 : 2);

/**
 * Every schedule asked for in a conversation. Each row opens under itself to what it sends and
 * where; it can be switched off and on, run again once stopped, and its conversation opened.
 * Schedules are set up and deleted in their conversation only.
 */
export function SchedulesSection() {
  const { t } = useI18n();
  const { list, loadFailed, setEnabled } = useSchedules();
  const hooks = list ? [...list.hooks].sort((a, b) => rank(a) - rank(b)) : null;
  return (
    <div className="dashboard-section">
      <h3 className="dashboard-section-title">{t('schedules.settings.title')}</h3>
      <p className="dashboard-section-description">{t('schedules.settings.description')}</p>
      {loadFailed ? (
        <p className="dashboard-section-description settings-section-error" role="alert">
          {t('schedules.settings.loadFailed')}
        </p>
      ) : null}
      {hooks === null ? (
        loadFailed ? null : (
          <p className="dashboard-section-description" role="status">
            {t('schedules.settings.loading')}
          </p>
        )
      ) : hooks.length === 0 ? (
        <p className="dashboard-section-description">{t('schedules.settings.empty')}</p>
      ) : (
        <ul className="schedule-list">
          {hooks.map((hook) => (
            <ScheduleRow
              key={hook.hook_id}
              hook={hook}
              recordingReady={list?.recording_ready ?? false}
              setEnabled={setEnabled}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

function ScheduleRow({
  hook,
  recordingReady,
  setEnabled,
}: {
  hook: HookView;
  recordingReady: boolean;
  setEnabled: (hookId: string, enabled: boolean) => Promise<boolean>;
}) {
  const { t, language } = useI18n();
  const navigate = useNavigate();
  const headingId = useId();
  const detailId = useId();
  // A stopped schedule waits on the user, so it opens with its reason in view.
  const [open, setOpen] = useState(hook.stopped_reason !== null);
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
  const next =
    isRunning(hook) && hook.next_fire_at ? formatInstant(hook.next_fire_at, language) : null;
  const asked = hook.created_from_title
    ? t('schedules.askedIn', { title: hook.created_from_title })
    : t('schedules.askedInUntitled');
  const reason = hook.stopped_reason
    ? t(
        hook.stopped_reason === 'turn_failed'
          ? 'schedules.stopped.turnFailed'
          : 'schedules.stopped.startFailed'
      )
    : isNotWatching(hook, recordingReady)
      ? t('schedules.notWatching')
      : null;

  return (
    <li className={open ? 'schedule-row schedule-row--open' : 'schedule-row'}>
      <button
        type="button"
        className="schedule-row__button"
        aria-expanded={open}
        aria-controls={detailId}
        onClick={() => setOpen(!open)}
      >
        <span className="schedule-summary">
          <span
            id={headingId}
            className={
              isRunning(hook) || hook.stopped_reason
                ? 'schedule-summary__heading'
                : 'schedule-summary__heading schedule-summary__heading--off'
            }
          >
            {hook.title ?? hook.message}
          </span>
          <span className="schedule-summary__meta">
            <span>{when}</span>
            {next ? <span>{t('schedules.next', { time: next })}</span> : null}
            <span>{asked}</span>
          </span>
        </span>
        <span className="schedule-state">
          <span
            className={
              hook.stopped_reason
                ? 'schedule-state__word schedule-state__word--turn'
                : 'schedule-state__word'
            }
          >
            {hook.stopped_reason
              ? t('schedules.stopped')
              : t(hook.enabled ? 'common.on' : 'common.off')}
          </span>
          {open ? (
            <ChevronDown size={16} aria-hidden="true" />
          ) : (
            <ChevronRight size={16} aria-hidden="true" />
          )}
        </span>
      </button>
      <div id={detailId} className="schedule-detail" hidden={!open}>
        {reason ? <p className="schedule-detail__reason">{reason}</p> : null}
        <dl className="schedule-fields">
          <dt>{t('overlay.approvalRequired.hook.when')}</dt>
          <dd>{when}</dd>
          {next ? (
            <>
              <dt>{t('schedules.detail.next')}</dt>
              <dd>{next}</dd>
            </>
          ) : null}
          <dt>{t('overlay.approvalRequired.hook.until')}</dt>
          <dd>
            {hook.until ? formatInstant(hook.until, language) : t('schedules.detail.untilNone')}
          </dd>
          <dt>{t('overlay.approvalRequired.hook.sendTo')}</dt>
          <dd>
            {t(
              hook.target_action_id === null
                ? 'overlay.approvalRequired.hook.newSession'
                : 'overlay.approvalRequired.hook.thisSession'
            )}
          </dd>
          <dt>{t('overlay.approvalRequired.hook.message')}</dt>
          <dd className="schedule-fields__message">{hook.message}</dd>
        </dl>
        <div className="schedule-detail__actions">
          {hook.stopped_reason ? null : (
            <span className="schedule-detail__switch">
              <span aria-hidden="true">{t('schedules.switch')}</span>
              <ScheduleSwitch
                on={hook.enabled}
                describedBy={headingId}
                disabled={pending}
                onChange={(on) => void change(on)}
              />
            </span>
          )}
          <span className="schedule-detail__buttons">
            <button
              type="button"
              className="history-filter-button"
              onClick={() =>
                navigate({
                  pathname: '/history',
                  search: historySelectionSearch(`action:${hook.created_from_action_id}`),
                })
              }
            >
              {t('schedules.openConversation')}
            </button>
            {hook.stopped_reason ? (
              <button
                type="button"
                className="history-filter-button todo-import__register"
                aria-describedby={headingId}
                disabled={pending}
                onClick={() => void change(true)}
              >
                {t('schedules.resume')}
              </button>
            ) : null}
          </span>
        </div>
        {failed ? (
          <p className="schedule-detail__failure settings-section-error" role="alert">
            {t('schedules.changeFailed')}
          </p>
        ) : null}
      </div>
    </li>
  );
}
