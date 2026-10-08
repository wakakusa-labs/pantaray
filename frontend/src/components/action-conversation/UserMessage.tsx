import type { ActionConversationUserItem } from '../../../electron/src/actions/actionConversationModel';
import { useI18n } from '@/context/useI18n';
import { AttachedFileChip } from './AttachedFileChip';
import { AttachedImages } from './AttachedImages';
import { ProjectRefText } from './ProjectRefText';
import { USER_MESSAGE_COPY as COPY } from './userMessageCopy';

export function UserItem({ item }: { item: ActionConversationUserItem }) {
  const { language } = useI18n();
  const copy = COPY[language];
  const content =
    item.source === 'canonical' ? item.entry.content : item.submission.request.message.content;
  const status = item.source === 'canonical' ? item.entry.status : item.submission.state;
  const images =
    item.source === 'canonical' ? item.entry.images : item.submission.request.message.images;
  const files =
    (item.source === 'canonical' ? item.entry.files : item.submission.request.message.files) ?? [];
  const projectRefs =
    item.source === 'canonical'
      ? item.entry.project_refs
      : (item.submission.request.message.project_refs ?? []);
  const state =
    status !== 'adopted' ? (
      <span
        className="action-conversation__state"
        role={item.source === 'optimistic' ? 'status' : undefined}
      >
        {copy.userStatus[status]}
      </span>
    ) : null;

  // What Pantaray's chat wrote when it handed this over: Pantaray's, never a user bubble.
  const chatNote = item.source === 'canonical' ? item.entry.chat_note : null;
  const hasUserPart = content !== null || images.length > 0 || files.length > 0;
  const note =
    chatNote !== null ? (
      <section className="action-conversation__chat-note" aria-label={copy.chatNote}>
        <span className="action-conversation__chat-note-label" aria-hidden="true">
          {copy.chatNote}
        </span>
        <p>{chatNote}</p>
        {hasUserPart ? null : state}
      </section>
    ) : null;
  if (!hasUserPart) return note ?? state;
  return (
    <>
      <article className="action-conversation__user" aria-label={copy.you}>
        {content !== null ? (
          <p>
            <ProjectRefText text={content} refs={projectRefs} />
          </p>
        ) : null}
        {images.length > 0 ? <AttachedImages images={images} copy={copy.userImages} /> : null}
        {files.length > 0 ? (
          <ul
            className="action-conversation__attachments"
            aria-label={copy.userFiles(files.length)}
          >
            {files.map((file, index) => (
              // A message may carry two files with the same name; the order is stable.
              <li key={index}>
                <AttachedFileChip name={file.name} byteSize={file.byte_size} />
              </li>
            ))}
          </ul>
        ) : null}
        {state}
      </article>
      {note}
    </>
  );
}
