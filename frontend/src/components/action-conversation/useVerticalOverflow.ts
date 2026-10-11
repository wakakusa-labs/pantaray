import { useLayoutEffect, useState, type RefObject } from 'react';

/**
 * 出力の箱が、描画した結果として実際に縦へはみ出しているか。キーボードで焦点を持つのは、
 * はみ出した箱だけにする。文字数や行数では決められない（折り返す幅しだいで、短くてもはみ出すし、
 * 長くても収まる）。幅が変われば折り返しも変わるので、寸法が動くたびに測り直す。収まっている
 * 箱まで焦点を持つと、会話を辿るタブ移動が無駄に増える。
 */
export function useVerticalOverflow(ref: RefObject<HTMLElement | null>, content: unknown): boolean {
  const [overflows, setOverflows] = useState(false);

  useLayoutEffect(() => {
    const element = ref.current;
    if (element === null) return;
    const measure = () => setOverflows(element.scrollHeight > element.clientHeight);
    measure();
    // ResizeObserver を持たない DOM 実装では、描画直後の 1 回の測定だけで済ませる。
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [ref, content]);

  return overflows;
}
