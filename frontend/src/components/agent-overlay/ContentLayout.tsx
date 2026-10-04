import styled from 'styled-components';
import { COLORS } from './Styled';
import { COLLAPSED_PREVIEW_MAX_HEIGHT_CSS } from './layoutMetrics';

/**
 * 本文コンテンツ・スクロール・補助UIのstyled群
 */
export const ContentFade = styled.div<{ $visible?: boolean }>`
  display: flex;
  flex-direction: column;
  flex: 0 1 auto;
  min-height: 0;
  opacity: ${(props) => (props.$visible ? 1 : 0)};
  /* 背景（600ms）と同じ遷移時間・カーブで同時に浮き上がる */
  transition: opacity 600ms cubic-bezier(0.25, 0.1, 0.25, 1);
`;

export const NotificationContent = styled.div`
  margin-top: 2px;
  margin-bottom: 0;
  -webkit-app-region: no-drag;
  -webkit-user-select: text;
  user-select: text;
  cursor: text;
  white-space: normal;
  color: ${COLORS.text};
  font-family: var(--font-sans);
  font-size: var(--text-body-size);
  font-weight: var(--weight-medium);
  line-height: var(--text-body-leading);
  letter-spacing: var(--text-body-tracking);
  word-break: break-word;
  background-color: transparent;
  border-top: 1px solid rgba(255, 255, 255, 0);
`;

export const AnswerArea = styled.div`
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid rgba(255, 255, 255, 0.2);
  -webkit-app-region: no-drag;
  -webkit-user-select: text;
  user-select: text;
  cursor: text;
  color: ${COLORS.text};
  font-family: var(--font-sans);
  font-size: var(--text-body-size);
  font-weight: var(--weight-medium);
  line-height: var(--text-body-leading);
  letter-spacing: var(--text-body-tracking);
  white-space: normal;
  word-break: break-word;
`;

export const UserActionArea = styled.div`
  margin-top: 8px;
  margin-bottom: 0;
  color: rgba(255, 255, 255, 0.8);
  font-size: var(--text-meta-size);
  font-style: italic;
  text-align: right;
`;

export const ScrollableContent = styled.div<{
  $collapsed?: boolean;
  $hasFooter?: boolean;
}>`
  -webkit-app-region: no-drag;
  -webkit-user-select: text;
  user-select: text;
  flex: 0 1 auto;
  min-height: 0;
  /*
   * 横スクロールは常に禁止する。
   * 許可すると、幅を超える子要素（画像・ツール出力など）が現れた瞬間に会話全体が
   * 横スクロール可能になり、トラックパッドの横方向の慣性や scrollIntoView で
   * 内容が左へずれてパネル外にクリップされる。
   *
   * ただし hidden は「操作できないスクロールコンテナ」でしかなく、scrollLeft の
   * 直接代入や scrollIntoView() は通ってしまう。横方向の切り落としは内側の
   * ContentInner（overflow-x: clip）が担い、ここは縦スクロールだけを持つ。
   */
  overflow-x: hidden;
  /*
   * 横の余白はパネル（PopupContainer の横 padding）が単独で持つ。ここで
   * スクロールバー用の帯を足すと、会話だけが右へ 5px 内側に寄り、パネル下端に
   * 固定した composer と行端が揃わなくなる（しかも畳んだ時だけ帯が消えるので、
   * 開閉でずれ幅が変わる）。スクロールバーは 5px 幅・スクロール中だけ可視で、
   * レイアウト幅を取らないオーバーレイ表示なので、帯は確保しない。
   */
  margin-bottom: ${(props) => {
    if (props.$collapsed) return '4px';
    return props.$hasFooter ? '8px' : '0';
  }};
  position: relative;
  z-index: 1;
  scrollbar-width: thin;
  scrollbar-color: transparent transparent;
  &::-webkit-scrollbar {
    width: 5px;
  }
  &::-webkit-scrollbar-track {
    background: transparent;
  }
  &::-webkit-scrollbar-thumb {
    background-color: transparent;
    border-radius: 999px;
  }
  &[data-scrolling='true'] {
    scrollbar-color: rgba(255, 255, 255, 0.12) transparent;
  }
  &[data-scrolling='true']::-webkit-scrollbar-thumb {
    background-color: rgba(255, 255, 255, 0.12);
  }
  &[data-scrolling='true']::-webkit-scrollbar-thumb:hover {
    background-color: rgba(255, 255, 255, 0.18);
  }
  max-height: ${(props) => (props.$collapsed ? COLLAPSED_PREVIEW_MAX_HEIGHT_CSS : 'none')};
  overflow-y: ${(props) => (props.$collapsed ? 'hidden' : 'auto')};
  transition:
    max-height 200ms ease,
    margin 160ms ease;
`;

export const ContentWrapper = styled.div`
  position: relative;
`;

/**
 * 会話本文を横方向に切り落とす層。
 *
 * ScrollableContent 側で overflow-x: clip は使えない。片方の軸が auto の場合
 * clip は hidden に落ちる（CSS Overflow 3 / Chromium 実測）ため、scrollLeft の代入や
 * scrollIntoView() が通ってしまい、会話だけが左へずれる。ここで先に切り落とせば
 * ScrollableContent の scrollWidth が幅を超えないので、横スクロールは発生し得ない。
 * 縦は visible のまま外側のスクロールに委ねる。
 */
export const ContentInner = styled.div`
  overflow-x: clip;
  overflow-y: visible;
`;

/**
 * composer をパネル下端に固定する枠。
 *
 * 会話は `ScrollableContent` の中だけを流れるので、履歴が伸びても composer は
 * ここに留まる。下地は `PopupContainer` の背景色がパネル全面に敷かれているので、
 * ここでは重ねない（重ねるとガラス層の掛からない帯になって浮く）。
 *
 * 横の余白も持たない。パネルの padding だけが横位置を決めるので、composer の行端は
 * どの幅でも、開いていても畳んでいても、会話の行端と一致する。
 *
 * composer が無い局面でも枠自体は残す。ウィンドウ高さを決める ResizeObserver は
 * 表示開始時に一度だけ観測対象を集めるので、後から現れる要素は観測されない。
 * 中身が無いときは `:empty` で畳み、余白も出さない。
 *
 * 会話との区切り線は引かない。composer 自身が枠を持つ面になったので、その上に
 * もう一本線を引くと境界が二重になる。
 */
export const ComposerDock = styled.div`
  flex: none;
  position: relative;
  z-index: 1;
  margin-top: 8px;
  -webkit-app-region: no-drag;

  &:empty {
    display: none;
  }
`;
