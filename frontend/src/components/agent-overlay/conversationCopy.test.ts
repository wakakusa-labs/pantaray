import { describe, expect, it } from 'vitest';

import type { ActionConversationPage } from '../../../electron/src/actions/actionContracts';
import { projectActionConversationView } from '../../../electron/src/actions/actionConversationModel';
import { formatConversationTranscript } from './conversationCopy';

type Run = ActionConversationPage['runs'][number];
type Entry = Run['entries'][number];

const COPY = {
  user: 'あなた',
  pantaray: 'Pantaray',
  images: (count: number) => `（画像 ${count} 枚）`,
};
const image = (index: number) => ({
  kind: 'image' as const,
  storage_path: `user-1/2026-09-29/${index}.png`,
});

const user = (
  step: number,
  sequence: number,
  content: string | null,
  suggestion: string | null = null,
  imageCount = 0
): Entry => ({
  step_kind: 'user',
  step_id: `user-${step}`,
  step_number: step,
  message_id: `message-${step}`,
  accepted_sequence: sequence,
  content,
  approved_suggestion: suggestion === null ? null : { suggestion_id: 'sug-1', content: suggestion },
  images: Array.from({ length: imageCount }, (_, index) => image(index)),
  project_refs: [],
  status: 'adopted',
});
const assistant = (step: number, content: string): Entry => ({
  step_kind: 'assistant',
  step_id: `assistant-${step}`,
  step_number: step,
  content,
});
const tool = (step: number): Entry => ({
  step_kind: 'tool',
  step_id: `tool-${step}`,
  step_number: step,
  label: 'read',
  status: 'success',
  outcome: 'completed',
  subject: 'notes.md',
  output_preview: null,
  output_available: true,
  images: [],
});
/** Entries are given oldest first and stored newest first, as the backend pages them. */
const run = (id: string, entries: Entry[], outcome: Pick<Run, 'final_output' | 'error'>): Run => ({
  run_id: id,
  status: outcome.error === null ? 'success' : 'error',
  started_at: '2026-09-29T00:00:00.000000Z',
  completed_at: '2026-09-29T00:01:00.000000Z',
  completion_event_id: `event-${id}`,
  entries: [...entries].reverse(),
  ...outcome,
});
const page = (runs: Run[], suggestion: string | null = null): ActionConversationPage => ({
  action: {
    action_id: 'action-1',
    suggestion_id: suggestion === null ? null : 'sug-1',
    approved_suggestion:
      suggestion === null ? null : { suggestion_id: 'sug-1', content: suggestion },
    status: 'error',
    latest_run_id: runs[0].run_id,
    resumable: false,
  },
  runs,
  unadopted_messages: [],
  next_cursor: null,
});

describe('formatConversationTranscript', () => {
  it('copies the approved Suggestion, messages, and outcomes in order without the folded work', () => {
    const first = run(
      'run-1',
      [
        user(1, 1, 'Keep the headings', 'I can **tidy** your notes.'),
        assistant(2, 'Reading the notes first.'),
        tool(3),
      ],
      { final_output: '- **Done**\n- Moved 3 files', error: null }
    );
    const pending = page([first], 'I can **tidy** your notes.');
    pending.unadopted_messages = [
      {
        ...(user(7, 2, 'And email me') as Extract<Entry, { step_kind: 'user' }>),
        step_number: null,
        status: 'pending',
      },
    ];

    expect(formatConversationTranscript(projectActionConversationView([pending]), COPY)).toBe(
      [
        'Pantaray\nI can **tidy** your notes.',
        'あなた\nKeep the headings',
        'Pantaray\n- **Done**\n- Moved 3 files',
        'あなた\nAnd email me',
      ].join('\n\n')
    );
  });

  it('keeps the commentary a run without a final answer leaves on screen', () => {
    const answered = run(
      'run-1',
      [user(1, 1, 'Summarize'), assistant(2, 'Reading the notes first.'), tool(3)],
      { final_output: 'Summarized.', error: null }
    );
    const failed = run(
      'run-2',
      [user(4, 2, 'Also archive them'), tool(5), assistant(6, 'Trying the archive.')],
      { final_output: null, error: { code: 'ACTION_FAILED', message: 'The archive failed.' } }
    );

    expect(
      formatConversationTranscript(projectActionConversationView([page([failed, answered])]), COPY)
    ).toBe(
      [
        'あなた\nSummarize',
        'Pantaray\nSummarized.',
        'あなた\nAlso archive them',
        'Pantaray\nTrying the archive.',
        'Pantaray\nThe archive failed.',
      ].join('\n\n')
    );
  });

  it('notes attached images, including on a message that has no text', () => {
    const view = projectActionConversationView([
      page(
        [
          run(
            'run-1',
            [
              user(1, 1, null, 'Review the screenshot', 2),
              tool(2),
              user(3, 2, 'And this one', null, 1),
            ],
            { final_output: 'Reviewed.', error: null }
          ),
        ],
        'Review the screenshot'
      ),
    ]);

    expect(formatConversationTranscript(view, COPY)).toBe(
      [
        'Pantaray\nReview the screenshot',
        'あなた\n（画像 2 枚）',
        'あなた\nAnd this one\n（画像 1 枚）',
        'Pantaray\nReviewed.',
      ].join('\n\n')
    );
  });

  it('keeps a message-only Suggestion that opens the conversation as Pantaray’s first message', () => {
    const view = projectActionConversationView([
      page([
        run(
          'run-1',
          [
            assistant(1, 'Your report is due tomorrow.'),
            user(2, 1, 'Draft it'),
            assistant(3, 'Reading the report.'),
          ],
          { final_output: 'Drafted.', error: null }
        ),
      ]),
    ]);

    expect(formatConversationTranscript(view, COPY)).toBe(
      'Pantaray\nYour report is due tomorrow.\n\nあなた\nDraft it\n\nPantaray\nDrafted.'
    );
  });
});
