import { useEffect, useId, useState } from 'react';
import type { ReactNode, RefObject } from 'react';
import { MessageCircle } from 'lucide-react';
import styled, { keyframes } from 'styled-components';
import { ComposerIconButton } from './OverlayComposer';

const ReplyButton = styled(ComposerIconButton)`
  width: 36px;
  height: 36px;
  border: 1px solid rgba(255, 255, 255, 0.16);
  border-radius: 50%;
  background: rgba(10, 14, 20, 0.16);
`;

const reveal = keyframes`
  from { grid-template-rows: 0fr; opacity: 0; }
  to { grid-template-rows: 1fr; opacity: 1; }
`;

const ExpandedInput = styled.div`
  display: grid;
  grid-template-rows: 1fr;
  min-height: 36px;
  animation: ${reveal} 200ms ease-out;

  > div {
    display: grid;
    gap: 8px;
    min-height: 0;
    overflow: hidden;
    /* Preserve the input's focus outline while clipping the reveal animation. */
    padding: 4px;
    margin: -4px;
  }

  @media (prefers-reduced-motion: reduce) {
    animation: none;
  }
`;

/** The caller keys this disclosure by Suggestion identity, so each new prompt starts quietly. */
export function SuggestionInputDisclosure({
  label,
  inputRef,
  children,
}: {
  label: string;
  inputRef: RefObject<HTMLTextAreaElement>;
  children: ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);
  const inputId = useId();
  useEffect(() => {
    if (expanded) inputRef.current?.focus();
  }, [expanded, inputRef]);

  return expanded ? (
    <ExpandedInput id={inputId}>
      <div>{children}</div>
    </ExpandedInput>
  ) : (
    <div id={inputId} style={{ display: 'flex', alignItems: 'center' }}>
      <ReplyButton
        type="button"
        aria-label={label}
        title={label}
        aria-expanded={false}
        aria-controls={inputId}
        onClick={() => setExpanded(true)}
      >
        <MessageCircle strokeWidth={1.75} aria-hidden />
      </ReplyButton>
    </div>
  );
}
