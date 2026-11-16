import { ArrowUp, Building2, CalendarDays, Plus } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';

import { TodoDateRangePicker } from '@/components/todo/TodoDateRangePicker';
import { compactDates, datesName } from '@/components/todo/todoDates';
import { isImeKey } from '@/components/ui/imeKey';
import { useI18n } from '@/context/useI18n';
import type { MessageKey } from '@/i18n/types';
import { compareProjects } from '@/pages/settings/components/workspaceSettingsModel';
import { todoRefusalMessage } from '@/todos/useTodoMutations';
import { oneLine } from '@/todos/todoText';

import type { WorkspaceSettings } from '../../../electron/src/settings/workspaceSettingsFetch';
import type { TodoView, TodoWriteResult } from '../../../electron/src/todos/todoContracts';
import AgentOverlayShell from './AgentOverlayShell';
import { ComposerAlert, ComposerAttachments } from './ComposerAttachments';
import {
  ComposerActions,
  ComposerForm,
  ComposerIconButton,
  ComposerPrimaryButton,
} from './OverlayComposer';
import { QuickTodoPicker } from './QuickTodoPicker';
import {
  ATTACHMENT_ACCEPT,
  addStagedAttachments,
  attachmentKey,
  attachmentPayload,
  canAttachMore,
  discardDocuments,
  pastedFiles,
  stageFiles,
  type AttachmentFailure,
  type ComposerAttachment,
} from './attachmentStaging';
import { useAgentOverlayController } from './useAgentOverlayController';
import { useOverlayHeaderDrag } from './useOverlayHeaderDrag';
import { useStandaloneComposerFocus } from './useStandaloneComposerFocus';

import './quickTodo.css';

/** Where a TODO goes: a project (in its organization), or no project in an organization or none. */
type Place = { organizationId: string | null; projectId: string | null };

const NOWHERE: Place = { organizationId: null, projectId: null };

/** Per viewer and only a convenience: the last place a TODO went from here is the next default. */
const PLACE_STORAGE_KEY = 'pantaray.quickTodo.place';

// Storage blocked or unavailable throws a DOMException: the next panel then starts with no place.
function readLastPlace(): Partial<Place> {
  try {
    return JSON.parse(localStorage.getItem(PLACE_STORAGE_KEY) ?? '{}');
  } catch (error) {
    if (error instanceof DOMException) return {};
    throw error;
  }
}

function saveLastPlace(place: Place) {
  try {
    localStorage.setItem(PLACE_STORAGE_KEY, JSON.stringify(place));
  } catch (error) {
    if (!(error instanceof DOMException)) throw error;
  }
}

/** The projects of an organization, or those in none, in the workspace's order. */
const projectsIn = (settings: WorkspaceSettings, organizationId: string | null) =>
  settings.projects
    .filter((project) =>
      organizationId === null
        ? project.organization_ids.length === 0
        : project.organization_ids.includes(organizationId)
    )
    .sort(compareProjects);

/** A place as far as it exists: an organization or project deleted, or not the organization's, is none. */
function placeIn(settings: WorkspaceSettings, wanted: Partial<Place>): Place {
  const organizationId =
    settings.organizations.find((item) => item.organization_id === wanted.organizationId)
      ?.organization_id ?? null;
  const projectId =
    projectsIn(settings, organizationId).find((item) => item.project_id === wanted.projectId)
      ?.project_id ?? null;
  return { organizationId, projectId };
}

/** This computer's calendar day, as the backend reads it (the OS time zone). */
function localToday() {
  const now = new Date();
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

type Attachments = {
  items: readonly ComposerAttachment[];
  /** Attach round trips still running; sending waits for them. */
  inFlight: number;
  failure: AttachmentFailure | null;
};

const NO_ATTACHMENTS: Attachments = { items: [], inFlight: 0, failure: null };

/**
 * The Option+Space panel: work handed over as a TODO. The text, its files and images, then one
 * row with ＋ to attach, its organization, its project (the organization's, or none), its start
 * (today at first) and due day (none at first), and send. A TODO is one line, as in the main
 * window's add bar: pasted lines are joined, Shift+Enter does nothing, and Enter sends unless an
 * IME takes it. A TODO that starts today is picked up at once, so the main window comes forward
 * on it and the panel closes; one that starts later, or has no start, is added and the panel
 * closes. The TODO holds its attachments until the Action that works on it takes them.
 */
export function QuickTodoPanel() {
  const ctrl = useAgentOverlayController(true);
  const headerDrag = useOverlayHeaderDrag();
  const { state, setContentReady, onClose, openTask } = ctrl;
  const { t, language } = useI18n();
  const fieldRef = useRef<HTMLTextAreaElement>(null);
  const attachInputRef = useRef<HTMLInputElement>(null);
  useStandaloneComposerFocus(true, fieldRef);
  useEffect(() => setContentReady(true), [setContentReady]);

  const [settings, setSettings] = useState<WorkspaceSettings | 'loading' | 'failed'>('loading');
  const [place, setPlace] = useState(NOWHERE);
  useEffect(() => {
    window.electron?.workspaceSettings?.get().then(
      (read) => {
        setSettings(read);
        setPlace(placeIn(read, readLastPlace()));
      },
      (error: unknown) => {
        console.error('Failed to read the organizations and projects:', error);
        setSettings('failed');
      }
    );
  }, []);
  const organizations = typeof settings === 'object' ? settings.organizations : [];
  const projects = typeof settings === 'object' ? projectsIn(settings, place.organizationId) : [];
  const organization = organizations.find((item) => item.organization_id === place.organizationId);
  const project = projects.find((item) => item.project_id === place.projectId);

  const [today] = useState(localToday);
  // 'today' is the backend's today when the TODO is sent; a null date is no start.
  const [start, setStart] = useState<'today' | { date: string | null }>('today');
  const [due, setDue] = useState<string | null>(null);
  const dates = { start: start === 'today' ? today : start.date, due };

  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const [failure, setFailure] = useState<MessageKey | null>(null);
  const [attachments, setAttachments] = useState(NO_ATTACHMENTS);
  const actions = window.electron?.actions;
  // A send whose answer never came may have added the TODO, so sending the same again reuses its
  // `client_todo_id` and the backend returns that TODO instead of adding a second one.
  const unansweredRef = useRef<{ key: string; clientTodoId: string } | null>(null);
  // The staged files of each send whose answer has not come, by its `client_todo_id`: a TODO
  // that was added may hold them, so they are never discarded from here. A send holds its files
  // from the moment it starts, so closing the panel meanwhile leaves them staged.
  const heldFilesRef = useRef(new Map<string, readonly string[]>());
  const line = text.trim();
  const canSend = !sending && attachments.inFlight === 0 && line !== '';

  const discard = (items: readonly ComposerAttachment[]) =>
    discardDocuments(
      actions,
      items.filter(
        (item) =>
          ![...heldFilesRef.current.values()].some((held) => held.includes(attachmentKey(item)))
      )
    );

  const attachFiles = async (picked: readonly File[]) => {
    if (!actions || sending || picked.length === 0) return;
    setAttachments((current) => ({ ...current, inFlight: current.inFlight + 1 }));
    const { accepted, failure: refused } = await stageFiles(actions, picked, attachments.items);
    setAttachments((current) => {
      const added = addStagedAttachments(actions, current.items, accepted, refused);
      return { items: added.attachments, inFlight: current.inFlight - 1, failure: added.failure };
    });
  };

  const removeAttachment = (removed: ComposerAttachment) => {
    discard([removed]);
    setAttachments((current) => ({
      ...current,
      items: current.items.filter((item) => attachmentKey(item) !== attachmentKey(removed)),
      failure: null,
    }));
  };

  // Closing destroys the panel's window, so its unsent files go too, but those a lost send held.
  const close = () => {
    discard(attachments.items);
    onClose();
  };

  const send = async () => {
    if (!canSend) return;
    const sentFiles = attachments.items.filter((item) => item.kind === 'file').map(attachmentKey);
    const body = {
      project_id: place.projectId,
      organization_id: place.projectId === null ? place.organizationId : null,
      text: line,
      ...(start === 'today' ? {} : { starts_on: start.date }),
      due: due === null ? null : { date: due, repeat: 'none' as const },
      ...(attachments.items.length > 0 ? attachmentPayload(attachments.items) : {}),
    };
    const key = JSON.stringify(body);
    const clientTodoId =
      unansweredRef.current?.key === key ? unansweredRef.current.clientTodoId : crypto.randomUUID();
    unansweredRef.current = { key, clientTodoId };
    heldFilesRef.current.set(clientTodoId, sentFiles);
    setSending(true);
    setFailure(null);
    let result: TodoWriteResult<TodoView>;
    try {
      const todos = window.electron?.todos;
      if (!todos) throw new Error('TODO bridge is unavailable.');
      result = await todos.create({ client_todo_id: clientTodoId, ...body });
    } catch (error) {
      console.error('Failed to add the TODO:', error);
      setFailure('todo.error.generic');
      setSending(false);
      return;
    }
    // This TODO's outcome is known now: added with these files, or refused without them.
    heldFilesRef.current.delete(clientTodoId);
    unansweredRef.current = null;
    setSending(false);
    if (result.kind === 'refused') {
      setFailure(todoRefusalMessage(result.code));
      return;
    }
    saveLastPlace(place);
    setText('');
    // The TODO holds them now.
    setAttachments(NO_ATTACHMENTS);
    const { todo_id, state: todoState } = result.result;
    if (todoState.kind === 'not_started' && todoState.reason !== 'starting') onClose();
    else openTask({ kind: 'todo', id: todo_id });
  };

  const composer = (
    <ComposerForm
      className="overlay-composer quick-todo"
      onSubmit={(event) => {
        event.preventDefault();
        void send();
      }}
      onDragOver={(event) => event.preventDefault()}
      onDrop={(event) => {
        event.preventDefault();
        void attachFiles(Array.from(event.dataTransfer.files));
      }}
    >
      <textarea
        ref={fieldRef}
        rows={1}
        className="quick-todo__field"
        aria-label={t('overlay.composer.label')}
        placeholder={t('overlay.composer.placeholder')}
        value={text}
        readOnly={sending}
        onChange={(event) => {
          setText(oneLine(event.target.value));
          setFailure(null);
        }}
        onKeyDown={(event) => {
          if (event.key !== 'Enter' || isImeKey(event)) return;
          event.preventDefault();
          if (!event.shiftKey) void send();
        }}
        onPaste={(event) => {
          const files = pastedFiles(event.clipboardData);
          if (sending || files.length === 0) return;
          // Taking the files means the field must not also insert their names.
          event.preventDefault();
          void attachFiles(files);
        }}
      />
      <ComposerAttachments
        attachments={attachments.items}
        failure={attachments.failure}
        readOnly={sending}
        focusAfterLastRemoved={fieldRef}
        onRemove={removeAttachment}
      />
      {settings === 'failed' ? (
        <ComposerAlert role="alert">{t('overlay.composer.mention.loadFailed')}</ComposerAlert>
      ) : null}
      {failure ? <ComposerAlert role="alert">{t(failure)}</ComposerAlert> : null}
      {ctrl.openTaskFailed ? (
        <ComposerAlert role="alert">{t('overlay.todo.openFailed')}</ComposerAlert>
      ) : null}
      <ComposerActions className="quick-todo__row">
        <input
          ref={attachInputRef}
          type="file"
          hidden
          multiple
          accept={ATTACHMENT_ACCEPT}
          onChange={(event) => {
            const files = Array.from(event.target.files ?? []);
            // Reset so picking the same file again still fires a change event.
            event.target.value = '';
            void attachFiles(files);
          }}
        />
        <ComposerIconButton
          type="button"
          className="quick-todo__attach"
          disabled={sending || !canAttachMore(attachments.items)}
          aria-label={t('overlay.composer.attach')}
          data-tooltip={t('overlay.composer.attach')}
          onClick={() => attachInputRef.current?.click()}
        >
          <Plus strokeWidth={1.75} aria-hidden="true" />
        </ComposerIconButton>
        <QuickTodoPicker
          label={t('overlay.todo.organization')}
          mark={
            organization?.icon_data_url ? (
              <img className="quick-todo__mark" src={organization.icon_data_url} alt="" />
            ) : organization ? (
              <span className="quick-todo__mark" aria-hidden="true">
                {Array.from(organization.display_name)[0].toUpperCase()}
              </span>
            ) : (
              <Building2 className="quick-todo__icon" strokeWidth={1.75} aria-hidden="true" />
            )
          }
          value={organization?.display_name ?? t('todo.rail.none')}
          choices={[
            ...organizations.map((item) => ({
              id: item.organization_id,
              name: item.display_name,
            })),
            { id: null, name: t('todo.rail.none') },
          ]}
          selected={place.organizationId}
          onSelect={(organizationId) =>
            // A project stays only when the organization picked holds it.
            setPlace(
              typeof settings === 'object'
                ? placeIn(settings, { organizationId, projectId: place.projectId })
                : NOWHERE
            )
          }
        />
        <QuickTodoPicker
          label={t('todo.add.project')}
          mark={<span className="quick-todo__dot" aria-hidden="true" />}
          value={project?.display_name ?? t('todo.noProject')}
          choices={[
            ...projects.map((item) => ({ id: item.project_id, name: item.display_name })),
            { id: null, name: t('todo.noProject') },
          ]}
          selected={place.projectId}
          onSelect={(projectId) => setPlace({ ...place, projectId })}
        />
        {/* The icon alone stands for the first dates; others show beside it, start first. */}
        <TodoDateRangePicker
          triggerLabel={datesName(t, language, today, dates)}
          tooltip
          triggerClassName="quick-todo__dates"
          dates={dates}
          today={today}
          opensOn="start"
          onChange={(next) => {
            setStart(next.start === today ? 'today' : { date: next.start });
            setDue(next.due);
          }}
        >
          <CalendarDays size={18} strokeWidth={1.75} aria-hidden="true" />
          {compactDates(t, today, dates)}
        </TodoDateRangePicker>
        <ComposerPrimaryButton
          type="submit"
          disabled={!canSend}
          aria-label={t('overlay.composer.send')}
          data-tooltip={t('overlay.composer.send')}
        >
          <ArrowUp strokeWidth={2.25} aria-hidden="true" />
        </ComposerPrimaryButton>
      </ComposerActions>
    </ComposerForm>
  );

  return (
    <AgentOverlayShell
      isVisible
      isContentVisible
      isExpanded={state.historyExpandOverride ?? state.isExpanded}
      content={null}
      suggestionText=""
      isSuggestionStreamFinished
      actionText=""
      isActionStreamFinished
      approvalUiState="hidden"
      showBusyIndicator={false}
      composer={composer}
      onToggleExpand={ctrl.onToggleExpand}
      onClose={close}
      onHeaderPointerDown={headerDrag.onHeaderPointerDown}
      onHeaderPointerMove={headerDrag.onHeaderPointerMove}
      onHeaderPointerUp={headerDrag.onHeaderPointerUp}
      onHeaderPointerCancel={headerDrag.onHeaderPointerCancel}
      headerRef={ctrl.headerRef}
      scrollableRef={ctrl.scrollableContentRef}
      contentInnerRef={ctrl.contentInnerRef}
      answerAreaRef={ctrl.answerAreaRef}
      composerRef={ctrl.composerRef}
      containerRef={ctrl.containerRef}
    />
  );
}
