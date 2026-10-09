// HeaderBar styled-only
import styled from 'styled-components';

const HeaderRow = styled.div`
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 4px;
  padding: 2px 0;
  margin-bottom: 2px;
  -webkit-app-region: no-drag;
  cursor: default;
  touch-action: none;
  position: relative;
  z-index: 1;
  min-height: 16px;
  /* テキスト量が多くてもヘッダーが詰まらないようにする */
  flex-shrink: 0;
`;

export default HeaderRow;

/**
 * The window surface's header is its title bar: the whole row drags the window, and the
 * buttons keep their own `no-drag`. The height and the left room for the macOS traffic
 * lights are the main window's (`Layout.css`: the 40px `.app-titlebar` row beside the 64px
 * `.app-rail`).
 */
export const WindowHeaderRow = styled(HeaderRow)`
  min-height: 40px;
  margin-bottom: 0;
  padding: 0 12px 0 64px;
  -webkit-app-region: drag;
`;

export const HeaderButtonGroup = styled.div`
  display: flex;
  gap: 4px;
  -webkit-app-region: no-drag;
  cursor: default;
  flex-shrink: 0;
`;
