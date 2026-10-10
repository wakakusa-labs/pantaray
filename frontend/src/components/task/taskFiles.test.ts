import { describe, expect, it } from 'vitest';

import type { ActionConversationPage } from '../../../electron/src/actions/actionContracts';
import { projectActionConversationView } from '../../../electron/src/actions/actionConversationModel';
import { createActionPage } from './actionTaskFixtures';
import { deriveTaskFiles } from './taskFiles';

type Entry = ActionConversationPage['runs'][number]['entries'][number];
type ToolEntry = Extract<Entry, { step_kind: 'tool' }>;

function patch(
  stepNumber: number,
  subject: string,
  operation: 'add' | 'update' | 'delete' = 'add'
): ToolEntry {
  return {
    step_kind: 'tool',
    step_id: `tool-${stepNumber}`,
    step_number: stepNumber,
    label: 'apply_patch',
    status: 'success',
    outcome: 'completed',
    subject,
    output_preview: null,
    output_available: true,
    images: [],
    file_edit: { operation, added_lines: 3, removed_lines: 0 },
  };
}

function files(entries: Entry[], finalOutput: string) {
  const page = createActionPage('act-1', 'success');
  page.runs[0].entries = entries;
  page.runs[0].final_output = finalOutput;
  return deriveTaskFiles(projectActionConversationView([page])).map(({ name, kind, label }) => ({
    name,
    kind,
    label,
  }));
}

describe('deriveTaskFiles', () => {
  it('lists the documents the answer links, once each, in order', () => {
    expect(
      files(
        [],
        [
          '見積書を作り直しました: [見積書_v3.html](pantaray-file:///work/quote/%E8%A6%8B%E7%A9%8D%E6%9B%B8_v3.html)',
          'もう一度: pantaray-file:///work/quote/%E8%A6%8B%E7%A9%8D%E6%9B%B8_v3.html',
          '送付メールは pantaray-file:///work/quote/mail.md 、単価表は pantaray-file:///work/quote/rates.xlsx。',
          '図は [chart](pantaray-file:///work/quote/chart.PNG) 、元データは pantaray-file:///work/quote/data.json',
          '要約 pantaray-file:///work/quote/summary.pdf',
        ].join('\n')
      )
    ).toEqual([
      { name: '見積書_v3.html', kind: 'html', label: 'HTML' },
      { name: 'mail.md', kind: 'markdown', label: 'MD' },
      { name: 'rates.xlsx', kind: 'app_only', label: 'XLSX' },
      { name: 'chart.PNG', kind: 'image', label: 'PNG' },
      { name: 'data.json', kind: 'text', label: 'JSON' },
      { name: 'summary.pdf', kind: 'pdf', label: 'PDF' },
    ]);
  });

  it('leaves code and files only a step names to the steps', () => {
    expect(
      files(
        // A step's subject is display text, not a path: main would refuse it.
        [patch(1, '/work/notes/plan.md'), patch(2, '/work/notes/summary.html', 'update')],
        'Done: [main.py](pantaray-file:///work/app/main.py), [Makefile](pantaray-file:///work/Makefile)'
      )
    ).toEqual([]);
  });

  it('keeps a linked path exactly, whitespace included', () => {
    const page = createActionPage('act-1', 'success');
    page.runs[0].final_output =
      '[見積書](pantaray-file:///work/%E8%A6%8B%E7%A9%8D%E6%9B%B8%20%20v3.md)';
    expect(deriveTaskFiles(projectActionConversationView([page])).map((file) => file.path)).toEqual(
      ['/work/見積書  v3.md']
    );
  });
});
