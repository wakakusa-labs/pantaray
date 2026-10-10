import { useId, useRef, useState } from 'react';
import type { KeyboardEvent, RefObject } from 'react';
import { flushSync } from 'react-dom';
import styled from 'styled-components';

import {
  findMentionTrigger,
  insertMention,
  rebaseMentions,
  removeMentionBefore,
  mentionOptionId,
  type ComposerEdit,
  type ComposerMention,
} from './composerMentions';
import {
  MENTION_PANEL_ROOM_PX,
  ProjectMentionList,
  type MentionOptionItem,
  type MentionPlacement,
} from './ProjectMentionList';
import type { WorkspaceSettings } from '../../../electron/src/settings/workspaceSettingsFetch';

/**
 * The textarea paints only the caret; the text itself is drawn by the backdrop behind it so a
 * project name can be coloured. Both share font, padding, wrapping and scrollbar gutter, which
 * keeps every glyph in the same place.
 */
const FieldStack = styled.div`
  position: relative;
`;

const sharedTextBox = `
  box-sizing: border-box;
  padding: 4px;
  font: inherit;
  /* The textarea's UA defaults; the backdrop must not inherit a different spacing. */
  letter-spacing: normal;
  word-spacing: normal;
  white-space: pre-wrap;
  overflow-wrap: break-word;
  scrollbar-gutter: stable;
`;

const Backdrop = styled.div`
  ${sharedTextBox}
  position: absolute;
  inset: 0;
  overflow: hidden;
  color: var(--text-primary);
  pointer-events: none;
`;

/** Project blue, shared with project names in sent messages; at least 4.5:1 on both surfaces. */
const MentionText = styled.span`
  color: #a8d0ff;
`;

/**
 * 1 行で始まり、入力に合わせて上へ伸びる本文欄。伸長は `field-sizing: content` に任せ、上限を
 * 超えた分は内部スクロールに回す。背後の層は絶対配置なので、高さを決めるのはこの欄だけ。
 */
const Textarea = styled.textarea<{ $composing: boolean }>`
  ${sharedTextBox}
  position: relative;
  display: block;
  width: 100%;
  field-sizing: content;
  /* 本文 8 行 + padding(4px×2) */
  max-height: calc(8lh + 8px);
  overflow-y: auto;
  resize: none;
  border: 0;
  /* During IME composition the native text (with its clause underlines) is shown instead. */
  color: ${({ $composing }) => ($composing ? 'var(--text-primary)' : 'transparent')};
  caret-color: var(--text-primary);
  background: transparent;

  /* Chromium's default placeholder grey, pinned so it never follows the transparent text. */
  &::placeholder {
    color: #757575;
  }

  &:focus-visible {
    outline: none;
  }
`;

type Catalog = 'loading' | 'failed' | WorkspaceSettings;

/**
 * One opening of the list. The workspace is read again on every opening, never cached, and the
 * side the list opens on is fixed for the opening so it never flips while the user types.
 */
type MentionSession = Readonly<{
  id: number;
  start: number;
  placement: MentionPlacement;
  catalog: Catalog;
}>;

/**
 * The files a paste attaches, or none when it pastes text. Copied text can carry a file along
 * (Quick Look adds the document it shows), and then the text is what was meant. A file copied
 * in Finder carries only its name as text, so that paste attaches the file.
 */
function pastedFiles(data: DataTransfer): File[] {
  const files = Array.from(data.files);
  const text = data.getData('text/plain').trim();
  if (files.length === 0 || text === '') return files;
  const names = new Set(files.map((file) => file.name));
  // Each line a pasted file's name, or a path ending in it.
  const namesOnly = text
    .split(/\s*[\r\n]+\s*/)
    .every((line) => names.has(line.slice(line.lastIndexOf('/') + 1)));
  return namesOnly ? files : [];
}

export function ComposerMessageField({
  id,
  textareaRef,
  value,
  mentions,
  readOnly,
  placeholder,
  invalid,
  describedBy,
  onChange,
  onKeyDown,
  onPasteFiles,
  onAddProject,
}: {
  id: string;
  textareaRef: RefObject<HTMLTextAreaElement>;
  value: string;
  mentions: readonly ComposerMention[];
  readOnly: boolean;
  placeholder: string;
  invalid: boolean;
  describedBy: string | undefined;
  onChange: (draft: string, mentions: ComposerMention[]) => void;
  onKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => void;
  /** Files pasted instead of text; a read-only field takes none. */
  onPasteFiles: (files: File[]) => void;
  /** "Add project": the Overlay brings the main window to Settings; the main window adds it. */
  onAddProject: () => void;
}) {
  const listId = useId();
  const backdropRef = useRef<HTMLDivElement>(null);
  const sessionCounterRef = useRef(0);
  const [caret, setCaret] = useState<number | null>(null);
  const [dismissedStart, setDismissedStart] = useState<number | null>(null);
  const [session, setSession] = useState<MentionSession | null>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const [composing, setComposing] = useState(false);

  const trigger = caret === null || readOnly ? null : findMentionTrigger(value, caret);
  const catalog = trigger && session?.start === trigger.start ? session.catalog : null;
  const settings = catalog === 'loading' || catalog === 'failed' ? null : catalog;
  const query = trigger?.query.toLowerCase() ?? '';
  const matches = (settings?.projects ?? []).filter((project) =>
    project.display_name.toLowerCase().includes(query)
  );
  const options: MentionOptionItem[] = [
    ...matches.map((project) => ({
      kind: 'project' as const,
      projectId: project.project_id,
      displayName: project.display_name,
      paths: (settings?.folders ?? [])
        .filter((folder) => folder.project_ids.includes(project.project_id))
        .map((folder) => folder.real_path),
    })),
    { kind: 'add' },
  ];
  // Nothing registered still offers "Add project"; a query that matches nothing closes the list.
  const open =
    catalog === 'failed' ||
    (settings !== null && (settings.projects.length === 0 || matches.length > 0));
  const active = Math.min(activeIndex, options.length - 1);

  const syncCaret = (draft: string, nextCaret: number) => {
    setCaret(nextCaret);
    const nextTrigger = readOnly ? null : findMentionTrigger(draft, nextCaret);
    if (nextTrigger === null) {
      setDismissedStart(null);
      setSession(null);
      return;
    }
    if (nextTrigger.start === dismissedStart || nextTrigger.start === session?.start) return;
    const sessionId = ++sessionCounterRef.current;
    // Float over the conversation when a full list fits above the composer frame; otherwise
    // open in flow below it, where the window grows downward and the frame stays put.
    const frameTop = textareaRef.current?.form?.getBoundingClientRect().top ?? 0;
    const placement = frameTop >= MENTION_PANEL_ROOM_PX ? 'above' : 'below';
    setSession({ id: sessionId, start: nextTrigger.start, placement, catalog: 'loading' });
    setActiveIndex(0);
    const settle = (result: Catalog) =>
      setSession((current) =>
        current?.id === sessionId ? { ...current, catalog: result } : current
      );
    const read = window.electron?.workspaceSettings?.get;
    if (!read) {
      settle('failed');
      return;
    }
    read().then(settle, () => settle('failed'));
  };

  const dismiss = (start: number) => {
    setDismissedStart(start);
    setSession(null);
  };

  const applyEdit = (edit: ComposerEdit) => {
    flushSync(() => {
      onChange(edit.draft, edit.mentions);
      syncCaret(edit.draft, edit.caret);
    });
    textareaRef.current?.setSelectionRange(edit.caret, edit.caret);
  };

  const pick = (index: number) => {
    const option = options[index];
    if (!trigger) return;
    if (option.kind === 'add') {
      onAddProject();
      dismiss(trigger.start);
      return;
    }
    const { kind: _kind, ...project } = option;
    applyEdit(insertMention(value, mentions, trigger, project));
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // かな漢字変換中のキーは変換のためのもの。一覧も送信も動かさない。
    if (event.nativeEvent.isComposing) return;
    if (open) {
      const step = event.key === 'ArrowDown' ? 1 : event.key === 'ArrowUp' ? -1 : 0;
      if (step !== 0) {
        event.preventDefault();
        setActiveIndex((active + step + options.length) % options.length);
        return;
      }
      if ((event.key === 'Enter' || event.key === 'Tab') && !event.shiftKey) {
        event.preventDefault();
        pick(active);
        return;
      }
      if (event.key === 'Escape' && trigger) {
        event.preventDefault();
        dismiss(trigger.start);
        return;
      }
    }
    const { selectionStart, selectionEnd } = event.currentTarget;
    const plainBackspace =
      event.key === 'Backspace' && !event.altKey && !event.metaKey && !event.ctrlKey;
    const edit =
      plainBackspace && selectionStart === selectionEnd
        ? removeMentionBefore(value, mentions, selectionStart)
        : null;
    if (edit) {
      event.preventDefault();
      applyEdit(edit);
      return;
    }
    onKeyDown(event);
  };

  // Plain text and project names alternate: even segments are plain, odd ones are mentions.
  const bounds = [0, ...mentions.flatMap((mention) => [mention.start, mention.end]), value.length];
  const segments = bounds.slice(1).map((end, index) => value.slice(bounds[index], end));

  return (
    <>
      <FieldStack>
        <Backdrop
          ref={backdropRef}
          aria-hidden="true"
          // visibility, not display: a hidden-by-display box would lose its synced scrollTop.
          style={{ visibility: composing ? 'hidden' : undefined }}
        >
          {segments.map((text, index) =>
            index % 2 ? <MentionText key={index}>{text}</MentionText> : text
          )}
          {/* A trailing newline needs a glyph after it to take up its line, as in the textarea. */}
          {'\u200b'}
        </Backdrop>
        <Textarea
          id={id}
          ref={textareaRef}
          rows={1}
          value={value}
          readOnly={readOnly}
          placeholder={placeholder}
          $composing={composing}
          aria-invalid={invalid}
          aria-describedby={describedBy}
          aria-autocomplete="list"
          aria-expanded={open}
          aria-controls={open ? listId : undefined}
          aria-activedescendant={open ? mentionOptionId(listId, active) : undefined}
          onChange={(event) => {
            const draft = event.target.value;
            onChange(draft, rebaseMentions(value, draft, mentions));
            syncCaret(draft, event.target.selectionStart);
          }}
          onSelect={(event) =>
            syncCaret(event.currentTarget.value, event.currentTarget.selectionStart)
          }
          onBlur={() => {
            setCaret(null);
            setSession(null);
          }}
          onScroll={(event) => {
            if (backdropRef.current) backdropRef.current.scrollTop = event.currentTarget.scrollTop;
          }}
          onCompositionStart={() => setComposing(true)}
          onCompositionEnd={() => setComposing(false)}
          onKeyDown={handleKeyDown}
          onPaste={(event) => {
            const files = pastedFiles(event.clipboardData);
            if (readOnly || files.length === 0) return;
            // Taking the files means the textarea must not also insert the text.
            event.preventDefault();
            onPasteFiles(files);
          }}
        />
      </FieldStack>
      {open ? (
        <ProjectMentionList
          id={listId}
          placement={session?.placement ?? 'below'}
          anchorRef={textareaRef}
          options={options}
          activeIndex={active}
          loadFailed={catalog === 'failed'}
          onPick={pick}
        />
      ) : null}
    </>
  );
}
