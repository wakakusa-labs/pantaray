import React from 'react';
import styled from 'styled-components';
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useLLMOutput } from '@llm-ui/react';
import type { BlockMatch, LLMOutputFallbackBlock } from '@llm-ui/react';
import { markdownLookBack } from '@llm-ui/markdown';
import type { ComponentPropsWithoutRef } from 'react';

import {
  BARE_FILE_URL_START,
  bareFileUrlAt,
  localPathFromPantarayFileHref,
} from '../../../electron/src/actions/pantarayFileLinks';

const MarkdownWrapper = styled.div`
  font-family: var(--font-sans);
  font-size: var(--text-body-size);
  font-weight: var(--text-body-weight);
  line-height: var(--text-body-leading);
  letter-spacing: var(--text-body-tracking);
  text-wrap: pretty;
  word-break: break-word;

  /* 日本語時はプロポーショナルメトリクスを活かす（英語への副作用を避けるため限定） */
  html:lang(ja) & {
    font-feature-settings: 'palt' 1;
  }

  p {
    margin: 0 0 0.45em 0;
  }
  p:last-child {
    margin-bottom: 0;
  }

  h1,
  h2,
  h3 {
    margin: 0.65em 0 0.25em;
    line-height: var(--text-heading-leading);
    font-weight: var(--text-heading-weight);
  }
  h1 {
    font-size: 20px;
  }
  h2 {
    font-size: var(--text-heading-size-lg);
  }
  h3 {
    font-size: var(--text-heading-size);
  }

  ul,
  ol {
    margin: 0.25em 0 0.55em;
    padding-left: 1.35em;
  }
  li {
    margin: 0.18em 0;
  }

  /* 画像の実寸でパネル幅を超えると、会話全体が横にずれて左端が切れる */
  img {
    max-width: 100%;
    height: auto;
  }

  blockquote {
    margin: 0.55em 0;
    padding: 8px 12px;
    border-left: 2px solid rgba(255, 255, 255, 0.2);
    background: rgba(255, 255, 255, 0.08);
    border-radius: 6px;
  }

  a {
    color: rgba(186, 230, 253, 0.95); /* info: #bae6fd */
    text-decoration: underline;
    text-decoration-color: rgba(186, 230, 253, 0.55);
  }
  a:hover {
    color: rgba(255, 255, 255, 0.95);
    text-decoration-color: rgba(255, 255, 255, 0.55);
  }

  code {
    font-family: var(--font-mono);
    font-size: 0.92em;
    background: rgba(255, 255, 255, 0.12);
    border-radius: 4px;
    padding: 0 3px;
  }

  pre {
    margin: 0.55em 0;
    padding: 8px;
    background: rgba(255, 255, 255, 0.1);
    border-radius: 6px;
    overflow: auto;
    line-height: 1.35;
  }
  pre code {
    background: transparent;
    border-radius: 0;
    padding: 0;
    font-size: var(--text-meta-size);
  }

  table {
    width: 100%;
    border-collapse: collapse;
    margin: 0.6em 0;
    font-size: var(--text-meta-size);
  }
  th,
  td {
    border: 1px solid rgba(255, 255, 255, 0.2);
    padding: 4px;
  }
  th {
    text-align: left;
    background: rgba(255, 255, 255, 0.05);
  }
`;

// The few mdast fields this pass reads and writes.
type MarkdownNode = {
  type: string;
  value?: string;
  url?: string;
  children?: MarkdownNode[];
  position?: { end: { offset?: number } };
};

/** ``openEnded``: the text ends where streamed output does, so its last URL may be cut. */
function linkBareFileUrls(text: string, openEnded: boolean): MarkdownNode[] | null {
  const nodes: MarkdownNode[] = [];
  let end = 0;
  for (
    let start = text.indexOf(BARE_FILE_URL_START);
    start !== -1;
    start = text.indexOf(BARE_FILE_URL_START, start + BARE_FILE_URL_START.length)
  ) {
    if (start < end) continue;
    const url = bareFileUrlAt(text, start);
    const localPath = localPathFromPantarayFileHref(url);
    if (!localPath || (openEnded && start + url.length === text.length)) continue;
    if (start > end) nodes.push({ type: 'text', value: text.slice(end, start) });
    const name = localPath.split('/').filter(Boolean).pop() ?? localPath;
    nodes.push({ type: 'link', url, children: [{ type: 'text', value: name }] });
    end = start + url.length;
  }
  if (nodes.length === 0) return null;
  if (end < text.length) nodes.push({ type: 'text', value: text.slice(end) });
  return nodes;
}

/** Links bare file URLs in prose; code and existing links hold no text node it reads. */
function remarkBareFileLinks(options: { streaming: boolean }) {
  return (tree: MarkdownNode, file: { value: unknown }): void => {
    // The block's source ends in a newline the stream has not written.
    const sourceLength = String(file.value).trimEnd().length;
    const visit = (node: MarkdownNode): void => {
      if (!node.children || node.type === 'link' || node.type === 'linkReference') return;
      node.children = node.children.flatMap((child) => {
        if (child.type === 'text' && child.value) {
          const openEnded = options.streaming && child.position?.end.offset === sourceLength;
          return linkBareFileUrls(child.value, openEnded) ?? [child];
        }
        visit(child);
        return [child];
      });
    };
    visit(tree);
  };
}

function markdownUrlTransform(url: string): string {
  if (url.startsWith('pantaray-file:///')) return url;
  return defaultUrlTransform(url);
}

const markdownComponents = {
  a: ({ href, children, ...props }: ComponentPropsWithoutRef<'a'>) => {
    const localPath = typeof href === 'string' ? localPathFromPantarayFileHref(href) : null;
    if (localPath) {
      return (
        <a
          href={href}
          {...props}
          onClick={(event) => {
            event.preventDefault();
            void window.electron?.actionFiles?.open({ path: localPath }).catch((error: unknown) => {
              console.error('Failed to reveal local file link', error);
            });
          }}
        >
          {children}
        </a>
      );
    }
    return (
      <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
        {children}
      </a>
    );
  },
};

export const MarkdownBlock: React.FC<{
  text: string;
  isStreamFinished?: boolean;
}> = ({ text, isStreamFinished }) => {
  const markdownFallbackBlock: LLMOutputFallbackBlock = {
    component: ({ blockMatch }: { blockMatch: BlockMatch }) => (
      <ReactMarkdown
        remarkPlugins={[remarkGfm, [remarkBareFileLinks, { streaming: !isStreamFinished }]]}
        components={markdownComponents}
        urlTransform={markdownUrlTransform}
      >
        {blockMatch.output}
      </ReactMarkdown>
    ),
    lookBack: markdownLookBack(),
  };
  const { blockMatches } = useLLMOutput({
    llmOutput: text ?? '',
    blocks: [],
    fallbackBlock: markdownFallbackBlock,
    isStreamFinished: Boolean(isStreamFinished),
  });

  return (
    <MarkdownWrapper>
      {blockMatches.map((blockMatch, index) => {
        const Component = blockMatch.block.component;
        return <Component key={index} blockMatch={blockMatch} />;
      })}
    </MarkdownWrapper>
  );
};

export default MarkdownBlock;
