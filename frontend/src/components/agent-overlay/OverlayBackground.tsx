// Overlay background styled-only
import styled from 'styled-components';

// Preserve the initial image framing and edge mask throughout the conversation.
const TINT_BLEED_PX = 66;

/** パネルの角丸。ティント層は自前の clip-path でこの形に戻す。 */
const PANEL_RADIUS_PX = 16;

/**
 * パネルが単独で持つ横の内側余白。
 *
 * 会話も composer もヘッダーも横位置はこれだけで決まる（`ContentLayout` 参照）。
 */
const PANEL_INLINE_PADDING_PX = 28;

/**
 * パネルの縁だけ地の色を薄く残す帯の幅。
 *
 * ティント層を端まで不透明にすると、パネルが壁紙の上に置いた板に見える。最外周の
 * わずかな帯だけ向こうを透かすと、以前の浮いた見え方が戻る。帯は文字の横余白
 * （`PANEL_INLINE_PADDING_PX`）より必ず内側で終わらせ、文字の裏には掛けない。
 */
const EDGE_FADE_PX = 22;

/** 帯の最外周の不透明度。ここから `EDGE_FADE_PX` 内側で完全な不透明に戻る。 */
const EDGE_FADE_ALPHA = 0.58;

/**
 * 明るいティントの上に一様に重ねる暗さ。白い本文の読みやすさのために面全体を少しだけ沈める。
 * 縁の透け方（`EDGE_FADE_ALPHA`）は変えない。
 */
const SHADE_ALPHA = 0.12;

/** 帯を折れ線で近似するときの分割数。曲線の折れ目が見えない程度に細かくする。 */
const EDGE_FADE_SEGMENTS = 8;

/**
 * 帯の途中の不透明度。`t` は帯の外端 0、内端 1。
 *
 * 直線で薄くすると、帯の内端で傾きが急に 0 になり、そこが濃淡の境目として見える。
 * smoothstep（`t^2 * (3 - 2t)`）は両端の傾きが 0 なので、外周でも内側の不透明面との
 * 継ぎ目でも折れ目が立たない。CSS のグラデーションは停止位置の間を直線で結ぶため、
 * この曲線を `EDGE_FADE_SEGMENTS` 等分して停止位置に落とす。
 */
const edgeFadeAlphaAt = (t: number) =>
  Number((EDGE_FADE_ALPHA + (1 - EDGE_FADE_ALPHA) * t * t * (3 - 2 * t)).toFixed(4));

/**
 * 片側ずつ帯を並べたグラデーション。`panelEdgePx` は層の端からパネルの端までの距離。
 *
 * 手前側は層の端から内側へ、奥側は同じ並びを鏡像にして末尾へ足す。両端の内側
 * （不透明度 1）どうしの間は不透明のまま残る。
 */
const edgeFadeGradient = (direction: 'to right' | 'to bottom', panelEdgePx: number) => {
  const nearStops: string[] = [];
  const farStops: string[] = [];
  for (let i = 0; i <= EDGE_FADE_SEGMENTS; i += 1) {
    const offsetPx = panelEdgePx + (EDGE_FADE_PX * i) / EDGE_FADE_SEGMENTS;
    const alpha = edgeFadeAlphaAt(i / EDGE_FADE_SEGMENTS);
    nearStops.push(`rgba(0, 0, 0, ${alpha}) ${offsetPx}px`);
    farStops.unshift(`rgba(0, 0, 0, ${alpha}) calc(100% - ${offsetPx}px)`);
  }
  return `linear-gradient(${direction}, ${[...nearStops, ...farStops].join(', ')})`;
};

/**
 * 四辺の帯を作るマスク。`panelEdgePx` は層の端からパネルの端までの距離。
 *
 * 横と縦の同じグラデーションを掛け合わせて四辺に同じ帯を出す。`mask-image` は
 * `filter` / `clip-path` のあとに効くので、ぼかして切り戻した結果の縁がそのまま薄くなる。
 */
const edgeFadeMask = (panelEdgePx: number) =>
  `${edgeFadeGradient('to right', panelEdgePx)}, ${edgeFadeGradient('to bottom', panelEdgePx)}`;

const reducedMotion =
  typeof window !== 'undefined' &&
  window.matchMedia &&
  window.matchMedia('(prefers-reduced-motion: reduce)').matches;

export const PopupContainer = styled.div<{
  $bg?: string;
  $isVisible?: boolean;
  $fadeMs?: number;
}>`
  display: flex;
  flex-direction: column;
  /* コンテンツに合わせて高さを決める（紺色の板問題を解消） */
  height: auto;
  max-height: 100vh;
  box-sizing: border-box;
  position: relative;
  /*
   * hidden ではなく clip。hidden はスクロールコンテナなので、パネル直下に置いた要素
   * （composer / フッター）が内側幅を超えた瞬間に、focus() や scrollIntoView() が
   * パネルごと右へスクロールさせ、全要素の左端が枠外へ切れる。clip はスクロール
   * コンテナを作らないため、角丸のクリップだけが残る。
   */
  overflow: clip;
  border-radius: ${PANEL_RADIUS_PX}px;
  isolation: isolate;
  /* 背景は ::before で描画するので、ここは透明 */
  background: transparent;
  /* 内側の縁だけ残す（外側の影は角丸が合わないため削除） */
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.1);
  /* 内側余白（タイトル・本文・フッターが端に張り付かないようにする） */
  padding: 4px ${PANEL_INLINE_PADDING_PX}px 14px;
  /* Suggestionの登場時は加速するease-in。Action切替ではズーム変更しない */
  will-change: transform;
  transform: ${(props) => (props.$isVisible ? 'scale(1)' : 'scale(0.985)')};
  transition: ${reducedMotion ? 'none' : 'transform 300ms cubic-bezier(0.4, 0, 1, 1)'};

  &::before {
    content: '';
    position: absolute;
    inset: -${TINT_BLEED_PX}px;
    clip-path: inset(${TINT_BLEED_PX}px round ${PANEL_RADIUS_PX}px);
    mask-image: ${edgeFadeMask(TINT_BLEED_PX)};
    mask-composite: intersect;
    background-image: ${(props) =>
      `linear-gradient(rgba(6, 10, 18, ${SHADE_ALPHA}), rgba(6, 10, 18, ${SHADE_ALPHA})),
        linear-gradient(135deg,
          rgba(228, 240, 255, 0.20),
          rgba(170, 205, 245, 0.10),
          rgba(205, 228, 255, 0.14)
        )${props.$bg ? `, url(${props.$bg})` : ''}`};
    opacity: ${(props) => (props.$isVisible ? '1' : '0')};
    transition: ${(props) => {
      const ms = props.$fadeMs === undefined ? 600 : Number(props.$fadeMs);
      if (reducedMotion) return 'none';
      if (ms <= 0) return 'none';
      return `opacity ${ms}ms cubic-bezier(0.25, 0.1, 0.25, 1)`;
    }};
    background-size: auto, auto, cover;
    background-position: center;
    background-repeat: no-repeat;
    filter: none;
    border: 0;
    box-shadow: none;
    pointer-events: none;
    z-index: 0;
  }
`;

export default PopupContainer;
