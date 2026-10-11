import { describe, expect, it } from 'vitest';

import { chipTitle, findWorkRefLinks, workRefLink } from './workRefLink';

describe('work reference links', () => {
  it('writes and finds the one format, ending at the first character no id holds', () => {
    const task = workRefLink('action', '3f86-1ab4');
    const suggestion = workRefLink('suggestion', 's_1');
    expect([task, suggestion]).toEqual(['pantaray://task/3f86-1ab4', 'pantaray://suggestion/s_1']);
    const text = `見て${task}について、${suggestion}. pantaray://folder/x`;
    expect(findWorkRefLinks(text)).toEqual([
      { kind: 'action', id: '3f86-1ab4', start: 2, end: 2 + task.length },
      {
        kind: 'suggestion',
        id: 's_1',
        start: text.indexOf(suggestion),
        end: text.indexOf(suggestion) + suggestion.length,
      },
    ]);
  });

  it('titles a chip with the first line, cut to 50 code points', () => {
    expect(chipTitle('\n  見積書を直す \n詳細は…')).toBe('見積書を直す');
    const long = '😀'.repeat(60);
    expect(Array.from(chipTitle(long))).toHaveLength(50);
    expect(chipTitle(long).endsWith('…')).toBe(true);
  });
});
