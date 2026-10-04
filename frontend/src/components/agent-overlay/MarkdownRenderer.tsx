import React from 'react';
import styled from 'styled-components';
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useLLMOutput } from '@llm-ui/react';
import type { BlockMatch, LLMOutputFallbackBlock } from '@llm-ui/react';
import { markdownLookBack } from '@llm-ui/markdown';
import type { ComponentPropsWithoutRef } from 'react';

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

function localPathFromPantarayFileHref(href: string): string | null {
  if (!href.startsWith('pantaray-file:///')) return null;
  try {
    const url = new URL(href);
    return decodeURIComponent(url.pathname);
  } catch {
    return null;
  }
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
        remarkPlugins={[remarkGfm]}
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
