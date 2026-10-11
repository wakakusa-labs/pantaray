import styled from 'styled-components';

// The primary decision is a solid fill so it reads as the action to take; the secondary one
// keeps a clear outline so it still reads as a button.
export const ApprovalDecisionButton = styled.button<{ $variant: 'primary' | 'secondary' }>`
  min-height: 36px;
  padding: 0 16px;
  border-radius: 10px;
  border: 1px solid
    ${({ $variant }) => ($variant === 'primary' ? '#7dabdc' : 'rgba(255, 255, 255, 0.28)')};
  background: ${({ $variant }) =>
    $variant === 'primary' ? '#7dabdc' : 'rgba(255, 255, 255, 0.08)'};
  color: ${({ $variant }) => ($variant === 'primary' ? '#0b0e14' : 'rgba(255, 255, 255, 0.92)')};
  font-family: var(--font-sans);
  font-size: var(--text-ui-size-sm);
  font-weight: var(--weight-semibold);
  cursor: pointer;
  -webkit-app-region: no-drag;

  &:hover:not([disabled]) {
    background: ${({ $variant }) =>
      $variant === 'primary' ? '#95bce6' : 'rgba(255, 255, 255, 0.14)'};
  }

  &:focus-visible {
    outline: 2px solid rgba(255, 255, 255, 0.7);
    outline-offset: 2px;
  }

  &:disabled {
    cursor: not-allowed;
    opacity: 0.62;
  }
`;
