import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { RefObject } from 'react';
import { createPortal } from 'react-dom';
import styled, { css } from 'styled-components';
import { Plus } from 'lucide-react';

import { useI18n } from '@/context/useI18n';
import { mentionOptionId, type ProjectMention } from './composerMentions';

const OPTION_HEIGHT_PX = 28;
const VISIBLE_PROJECT_ROWS = 7;
const FLOATING_PADDING_PX = 4;
const FLOATING_GAP_PX = 6;

/** Room a full panel needs above the composer frame: project rows, "Add project", chrome, gap. */
export const MENTION_PANEL_ROOM_PX =
  OPTION_HEIGHT_PX * (VISIBLE_PROJECT_ROWS + 1) + FLOATING_PADDING_PX * 2 + 2 + FLOATING_GAP_PX;

export type MentionPlacement = 'above' | 'below';

/**
 * One dark panel for both sides, portalled into a host placed by this component: in body and
 * fixed over the conversation (`above`), or in flow right after the composer frame (`below`),
 * where the window grows downward and the frame stays put.
 */
const MentionPanel = styled.div<{ $placement: MentionPlacement }>`
  display: grid;
  gap: 4px;
  box-sizing: border-box;
  padding: ${FLOATING_PADDING_PX}px;
  border: 1px solid var(--border-color);
  border-radius: 12px;
  /* Opaque so the conversation underneath does not show through. */
  background: rgb(26, 30, 36);
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
  font-size: var(--text-body-size);
  -webkit-app-region: no-drag;

  ${({ $placement }) =>
    $placement === 'above'
      ? css`
          position: fixed;
          z-index: 1000;
        `
      : css`
          margin-top: ${FLOATING_GAP_PX}px;
        `}
`;

/** Only the projects scroll; "Add project" stays pinned under them as the last option. */
const ProjectScroller = styled.div`
  max-height: ${OPTION_HEIGHT_PX * VISIBLE_PROJECT_ROWS}px;
  overflow-y: auto;
`;

const MentionOption = styled.div`
  display: flex;
  align-items: center;
  gap: 6px;
  box-sizing: border-box;
  height: ${OPTION_HEIGHT_PX}px;
  padding: 0 8px;
  border-radius: 8px;
  color: var(--text-primary);
  cursor: pointer;

  span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  svg {
    flex: none;
    width: 14px;
    height: 14px;
  }

  &:hover,
  &[aria-selected='true'] {
    background: rgba(255, 255, 255, 0.1);
  }
`;

const MentionFailure = styled.p`
  margin: 0;
  padding: 0 8px;
  color: rgba(254, 202, 202, 0.95);
  font-size: var(--text-meta-size);
`;

export type MentionOptionItem = Omit<ProjectMention, 'start' | 'end'> | Readonly<{ kind: 'add' }>;

export function ProjectMentionList({
  id,
  placement,
  anchorRef,
  options,
  activeIndex,
  loadFailed,
  onPick,
}: {
  id: string;
  placement: MentionPlacement;
  /** The composer text field; the floating list aligns with its form's frame. */
  anchorRef: RefObject<HTMLTextAreaElement>;
  /** Matching projects followed by "Add project", always last. */
  options: readonly MentionOptionItem[];
  activeIndex: number;
  loadFailed: boolean;
  onPick: (index: number) => void;
}) {
  const { t } = useI18n();
  const panelRef = useRef<HTMLDivElement>(null);
  const [host] = useState(() => document.createElement('div'));
  useEffect(() => {
    document.getElementById(mentionOptionId(id, activeIndex))?.scrollIntoView({ block: 'nearest' });
  }, [id, activeIndex]);
  useLayoutEffect(() => {
    const frame = anchorRef.current?.form;
    if (placement === 'below') frame?.after(host);
    else document.body.append(host);
    return () => host.remove();
  }, [anchorRef, host, placement]);
  useLayoutEffect(() => {
    if (placement !== 'above') return;
    const place = () => {
      const frame = anchorRef.current?.form?.getBoundingClientRect();
      const panel = panelRef.current;
      if (!frame || !panel) return;
      panel.style.left = `${frame.left}px`;
      panel.style.width = `${frame.width}px`;
      panel.style.bottom = `${window.innerHeight - frame.top + FLOATING_GAP_PX}px`;
    };
    place();
    window.addEventListener('resize', place);
    return () => window.removeEventListener('resize', place);
  });

  const renderOption = (option: MentionOptionItem, index: number) => (
    <MentionOption
      key={option.kind === 'project' ? option.projectId : 'add'}
      id={mentionOptionId(id, index)}
      role="option"
      aria-selected={index === activeIndex}
      onClick={() => onPick(index)}
    >
      {option.kind === 'add' ? <Plus strokeWidth={1.75} aria-hidden /> : null}
      <span>
        {option.kind === 'project' ? option.displayName : t('overlay.composer.mention.addProject')}
      </span>
    </MentionOption>
  );

  const panel = (
    // Keep focus (and the caret) in the text field while the pointer picks an option.
    <MentionPanel
      ref={panelRef}
      $placement={placement}
      onMouseDown={(event) => event.preventDefault()}
    >
      {loadFailed ? (
        <MentionFailure role="alert">{t('overlay.composer.mention.loadFailed')}</MentionFailure>
      ) : null}
      <div id={id} role="listbox" aria-label={t('overlay.composer.mention.label')}>
        <ProjectScroller role="presentation">
          {options.slice(0, -1).map(renderOption)}
        </ProjectScroller>
        {renderOption(options[options.length - 1], options.length - 1)}
      </div>
    </MentionPanel>
  );
  return createPortal(panel, host);
}
