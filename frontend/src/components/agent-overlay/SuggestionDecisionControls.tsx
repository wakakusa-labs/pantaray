import type { ReactNode, RefObject } from 'react';
import styled from 'styled-components';
import { useI18n } from '@/context/useI18n';
import { AcceptButton, RejectButton } from './ActionButtons';
import Footer, { FooterActionGroup } from './FooterActions';
import { SuggestionInputDisclosure } from './SuggestionInputDisclosure';

const DecisionFooter = styled(Footer)`
  flex-direction: column;
  align-items: stretch;
`;

export type SuggestionDecision = {
  input: ReactNode;
  inputRef: RefObject<HTMLTextAreaElement>;
  canAccept: boolean;
  accept: () => void;
  errorMessage: string | null;
};

type SuggestionDecisionControlsProps = {
  decision: SuggestionDecision;
  isVisible: boolean;
  isBusy: boolean;
  compact: boolean;
  footerRef?: RefObject<HTMLDivElement>;
  onReject?: () => void;
};

export function SuggestionDecisionControls({
  decision,
  isVisible,
  isBusy,
  compact,
  footerRef,
  onReject,
}: SuggestionDecisionControlsProps) {
  const { t } = useI18n();
  const actions = (
    <FooterActionGroup style={{ justifyContent: 'flex-end' }}>
      {onReject ? (
        <RejectButton
          onClick={onReject}
          $isBusy={isBusy}
          $visible={isVisible}
          aria-label={t('overlay.dismissSuggestion')}
          title={t('overlay.dismissTitle')}
        >
          {t('overlay.dismiss')}
        </RejectButton>
      ) : null}
      <AcceptButton
        onClick={decision.accept}
        disabled={!decision.canAccept}
        $isBusy={isBusy}
        $visible={isVisible}
      >
        {t('overlay.accept')}
      </AcceptButton>
    </FooterActionGroup>
  );
  return (
    <DecisionFooter ref={footerRef} $compact={compact}>
      <SuggestionInputDisclosure
        label={t('overlay.supplement.label')}
        inputRef={decision.inputRef}
        collapsedActions={actions}
      >
        {decision.input}
        {actions}
      </SuggestionInputDisclosure>
      {decision.errorMessage && <p role="alert">{decision.errorMessage}</p>}
    </DecisionFooter>
  );
}
