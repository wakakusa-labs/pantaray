import { Fragment } from 'react';
import styled from 'styled-components';

import type { MessageKey } from '@/i18n/types';
import type { ActionApprovalBlocker } from '../../../electron/src/actions/actionLiveCore';
import { ApprovalDecisionButton } from './ApprovalDecisionButton';
import { buildApprovalDisplay, type ApprovalDecision } from './approvalDisplayModel';

const ApprovalPanelCard = styled.div`
  margin: 8px 0 12px;
  padding: 14px;
  border-radius: 16px;
  border: 1px solid rgba(255, 255, 255, 0.12);
  background: rgba(9, 12, 20, 0.6);
`;

const ApprovalPanelTitle = styled.div`
  font-family: var(--font-sans);
  font-size: var(--text-ui-size-sm);
  font-weight: var(--weight-bold);
  color: rgba(255, 255, 255, 0.94);
  letter-spacing: var(--ui-label-tracking);
`;

const ApprovalOperationText = styled.p`
  margin: 10px 0 0;
  font-family: var(--font-sans);
  font-size: var(--text-ui-size-sm);
  line-height: 1.5;
  color: rgba(255, 255, 255, 0.9);
`;

const ApprovalFolderPath = styled.p`
  margin: 4px 0 0;
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.42;
  color: rgba(255, 255, 255, 0.62);
  word-break: break-all;
`;

const ApprovalFolderList = styled.ul`
  margin: 0;
  padding: 0;
  list-style: none;
`;

const ApprovalHint = styled.p`
  margin: 12px 0 0;
  font-family: var(--font-sans);
  font-size: var(--text-ui-size-sm);
  line-height: 1.5;
  color: rgba(255, 255, 255, 0.66);
`;

const ApprovalHintLink = styled.button`
  display: inline;
  margin: 0;
  padding: 0;
  border: 0;
  background: none;
  font: inherit;
  color: rgba(255, 255, 255, 0.9);
  text-decoration: underline;
  text-underline-offset: 2px;
  cursor: pointer;
  -webkit-app-region: no-drag;

  &:hover {
    color: #fff;
  }

  &:focus-visible {
    outline: 2px solid rgba(255, 255, 255, 0.7);
    outline-offset: 2px;
    border-radius: 4px;
  }
`;

const ApprovalDetailGroup = styled.div`
  margin-top: 12px;
`;

const ApprovalDetailLabel = styled.div`
  font-family: var(--font-sans);
  font-size: 11px;
  font-weight: var(--weight-semibold);
  color: rgba(255, 255, 255, 0.56);
  letter-spacing: var(--text-meta-tracking);
  text-transform: uppercase;
`;

const ApprovalCodeBlock = styled.pre`
  margin: 6px 0 0;
  padding: 10px 12px;
  border-radius: 10px;
  border: 1px solid rgba(255, 255, 255, 0.1);
  background: rgba(0, 0, 0, 0.24);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.42;
  color: rgba(255, 255, 255, 0.86);
  white-space: pre-wrap;
  word-break: break-word;
`;

const ApprovalDisclosure = styled.details`
  margin-top: 12px;
`;

const ApprovalDisclosureSummary = styled.summary`
  font-family: var(--font-sans);
  font-size: var(--text-ui-size-sm);
  color: rgba(255, 255, 255, 0.66);
  cursor: pointer;
  -webkit-app-region: no-drag;

  &:focus-visible {
    outline: 2px solid rgba(255, 255, 255, 0.7);
    outline-offset: 2px;
    border-radius: 4px;
  }
`;

const ApprovalDetailList = styled.dl`
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 5px 10px;
  margin: 10px 0 0;
`;

const ApprovalDetailTerm = styled.dt`
  font-family: var(--font-sans);
  font-size: var(--text-ui-size-sm);
  font-weight: var(--weight-semibold);
  color: rgba(255, 255, 255, 0.54);
`;

const ApprovalDetailValue = styled.dd`
  margin: 0;
  font-family: var(--font-sans);
  font-size: var(--text-ui-size-sm);
  line-height: 1.45;
  color: rgba(255, 255, 255, 0.78);
  word-break: break-word;
`;

const ApprovalActionRow = styled.div`
  display: flex;
  gap: 8px;
  margin-top: 14px;
  flex-wrap: wrap;
`;

const ApprovalErrorText = styled.p`
  margin: 10px 0 0;
  font-family: var(--font-sans);
  font-size: var(--text-ui-size-sm);
  line-height: 1.5;
  color: rgba(254, 202, 202, 0.95);
`;

type ApprovalPanelProps = {
  approvalPanel: ActionApprovalBlocker;
  approvalErrorMessage?: string | null;
  isSubmittingApproval: boolean;
  onDecide?: (decision: ApprovalDecision) => void;
  onOpenWorkspaceSettings?: () => void;
  t: (key: MessageKey, vars?: Record<string, string | number>) => string;
};

const DECISION_OPTIONS: readonly {
  decision: ApprovalDecision;
  variant: 'primary' | 'secondary';
}[] = [
  { decision: 'denied', variant: 'secondary' },
  { decision: 'approved_once', variant: 'primary' },
  { decision: 'approved_for_conversation', variant: 'secondary' },
];

export function ApprovalPanel({
  approvalPanel,
  approvalErrorMessage = null,
  isSubmittingApproval,
  onDecide,
  onOpenWorkspaceSettings,
  t,
}: ApprovalPanelProps) {
  const display = buildApprovalDisplay(approvalPanel, t);
  const folderPaths =
    display.outsideWorkspace === null ? null : display.outsideWorkspace.folders.length === 1 ? (
      <ApprovalFolderPath>{display.outsideWorkspace.folders[0].path}</ApprovalFolderPath>
    ) : (
      <ApprovalFolderList>
        {display.outsideWorkspace.folders.map((folder) => (
          <ApprovalFolderPath as="li" key={folder.path}>
            {folder.path}
          </ApprovalFolderPath>
        ))}
      </ApprovalFolderList>
    );

  const toolDetails = (
    <ApprovalDetailGroup>
      {display.primaryValue ? (
        <>
          <ApprovalDetailLabel>{t(display.primaryLabelKey)}</ApprovalDetailLabel>
          <ApprovalCodeBlock>{display.primaryValue}</ApprovalCodeBlock>
        </>
      ) : null}
      {display.details.length > 0 ? (
        <ApprovalDetailList>
          {display.details.map((detail, index) => (
            <Fragment key={`${detail.labelKey}-${index}`}>
              <ApprovalDetailTerm>{t(detail.labelKey)}</ApprovalDetailTerm>
              <ApprovalDetailValue>{detail.value}</ApprovalDetailValue>
            </Fragment>
          ))}
        </ApprovalDetailList>
      ) : null}
    </ApprovalDetailGroup>
  );

  const question = display.reason ?? t(display.operationKey, display.operationVars);
  let body;
  if (display.runsOutsideSandbox) {
    // The model says what the run is for; Pantaray, not the model, says what
    // allowing it means. The command stays one click away.
    body = (
      <>
        <ApprovalDetailGroup>
          <ApprovalDetailLabel>
            {t('overlay.approvalRequired.outsideSandbox.purpose')}
          </ApprovalDetailLabel>
          <ApprovalOperationText>{question}</ApprovalOperationText>
        </ApprovalDetailGroup>
        <ApprovalDetailGroup>
          <ApprovalDetailLabel>
            {t('overlay.approvalRequired.outsideSandbox.effect')}
          </ApprovalDetailLabel>
          <ApprovalOperationText>
            {t('overlay.approvalRequired.outsideSandbox.effectDescription')}
          </ApprovalOperationText>
        </ApprovalDetailGroup>
        <ApprovalDisclosure>
          <ApprovalDisclosureSummary>
            {t('overlay.approvalRequired.details')}
          </ApprovalDisclosureSummary>
          {toolDetails}
        </ApprovalDisclosure>
      </>
    );
  } else if (display.reason) {
    // A reason is for readers who cannot read commands: it leads, the folders
    // follow, and the command stays one click away.
    body = (
      <>
        <ApprovalOperationText>{question}</ApprovalOperationText>
        {display.usesLoginEnvironment ? (
          <ApprovalOperationText>
            {t('overlay.approvalRequired.loginEnvironmentNotice')}
          </ApprovalOperationText>
        ) : null}
        {folderPaths ? (
          <ApprovalDetailGroup>
            <ApprovalDetailLabel>
              {t('overlay.approvalRequired.outsideWorkspace.folders')}
            </ApprovalDetailLabel>
            {folderPaths}
          </ApprovalDetailGroup>
        ) : null}
        <ApprovalDisclosure>
          <ApprovalDisclosureSummary>
            {t('overlay.approvalRequired.details')}
          </ApprovalDisclosureSummary>
          {toolDetails}
        </ApprovalDisclosure>
      </>
    );
  } else {
    body = (
      <>
        <ApprovalOperationText>{question}</ApprovalOperationText>
        {folderPaths}
        {toolDetails}
      </>
    );
  }

  return (
    <ApprovalPanelCard>
      <ApprovalPanelTitle>
        {t(
          display.runsOutsideSandbox
            ? 'overlay.approvalRequired.outsideSandbox.title'
            : 'overlay.approvalRequired.title'
        )}
      </ApprovalPanelTitle>
      {body}
      {approvalErrorMessage ? <ApprovalErrorText>{approvalErrorMessage}</ApprovalErrorText> : null}
      <ApprovalActionRow>
        {onDecide
          ? DECISION_OPTIONS.map(({ decision, variant }) => {
              const labelKey = display.decisionLabelKeys[decision];
              return labelKey ? (
                <ApprovalDecisionButton
                  key={decision}
                  onClick={() => onDecide(decision)}
                  disabled={isSubmittingApproval}
                  type="button"
                  $variant={variant}
                >
                  {t(labelKey)}
                </ApprovalDecisionButton>
              ) : null;
            })
          : null}
      </ApprovalActionRow>
      {display.outsideWorkspace && onOpenWorkspaceSettings ? (
        <ApprovalHint>
          {t(display.outsideWorkspace.hintKey)}{' '}
          <ApprovalHintLink type="button" onClick={onOpenWorkspaceSettings}>
            {t('overlay.approvalRequired.outsideWorkspace.openSettings')}
          </ApprovalHintLink>
        </ApprovalHint>
      ) : null}
    </ApprovalPanelCard>
  );
}
