import styled from 'styled-components';
import { COLORS } from './Styled';

/*
 * The press and hover scale the icon, not the button: a transform or clipping on the button would
 * trap its tooltip (iconTooltip.css) inside it.
 */
export const HeaderIconButton = styled.button<{ $visible?: boolean }>`
  position: relative;
  background: transparent;
  border: none;
  border-radius: 8px;
  padding: 8px;
  color: ${COLORS.buttonText};
  cursor: pointer;
  opacity: ${(props) => (props.$visible ? 1 : 0)};
  transition:
    opacity 0.6s cubic-bezier(0.25, 0.1, 0.25, 1),
    color 0.15s cubic-bezier(0.4, 0, 0.2, 1),
    background 0.15s cubic-bezier(0.4, 0, 0.2, 1);
  line-height: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  -webkit-app-region: no-drag;

  &:disabled {
    opacity: 0.55;
    cursor: not-allowed;
    background: transparent;
  }

  &::before {
    content: '';
    position: absolute;
    inset: 0;
    border-radius: inherit;
    background: radial-gradient(circle, rgba(255, 255, 255, 0.3) 0%, transparent 70%);
    opacity: 0;
    transform: scale(0);
    transition:
      opacity 0.2s ease,
      transform 0.2s ease;
    pointer-events: none;
  }

  &:hover {
    color: rgba(255, 255, 255, 1);
    background: rgba(255, 255, 255, 0.08);
  }

  &:disabled:hover {
    color: ${COLORS.buttonText};
    background: transparent;
  }

  &:active {
    background: rgba(255, 255, 255, 0.12);
    &::before {
      opacity: 1;
      transform: scale(1);
    }
  }

  &:disabled:active {
    background: transparent;
  }

  &:focus-visible {
    outline: 2px solid rgba(255, 255, 255, 0.6);
    outline-offset: 2px;
    background: rgba(255, 255, 255, 0.08);
  }

  svg {
    display: block;
    width: 16px;
    height: 16px;
    transition: transform 0.1s cubic-bezier(0.4, 0, 0.2, 1);
  }

  &:hover svg {
    transform: scale(1.05);
  }

  &:active svg {
    transform: scale(0.98);
  }

  &:disabled svg {
    transform: none;
  }
`;
