import styled from 'styled-components';
import { FileText } from 'lucide-react';

import { formatBytes } from '@/lib/formatBytes';

/**
 * One attached document: its name and size, framed like an image thumbnail. The composer and
 * the sent message show the same chip, so what the user attached is what the conversation shows.
 */
export const FileChip = styled.span`
  box-sizing: border-box;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: center;
  column-gap: 8px;
  max-width: 240px;
  padding: 8px 12px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--surface-bg);

  svg {
    grid-row: span 2;
    width: 20px;
    height: 20px;
    color: var(--text-secondary);
  }
`;

const FileName = styled.span`
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
`;

const FileSize = styled.span`
  color: var(--text-secondary);
  font-size: var(--text-meta-size);
`;

export function AttachedFileChip({ name, byteSize }: { name: string; byteSize: number }) {
  return (
    <FileChip>
      <FileText strokeWidth={1.75} aria-hidden />
      <FileName title={name}>{name}</FileName>
      <FileSize>{formatBytes(byteSize)}</FileSize>
    </FileChip>
  );
}
