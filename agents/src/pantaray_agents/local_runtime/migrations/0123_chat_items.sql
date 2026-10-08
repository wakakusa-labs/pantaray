-- The user's single chat. Items are appended and never rewritten: a reply, an
-- event or a failure is a new row, so the chat reads back as it was written.
-- Rows leave only with their user, through the cascade from `users`.
--
-- `payload` is the item's content as the code's closed union validates it; the
-- columns beside it repeat only what a constraint or a lookup needs, and the
-- CHECK keeps them equal to the payload.
CREATE TABLE chat_items (
    sequence INTEGER PRIMARY KEY,
    item_id TEXT NOT NULL CHECK (length(item_id) > 0),
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    -- The appender's idempotency key: the client's id for a user message, an id
    -- derived from its source for any other item.
    message_id TEXT NOT NULL CHECK (length(message_id) > 0),
    kind TEXT NOT NULL CHECK (kind IN (
        'user_message', 'assistant_message', 'suggestion_event', 'action_event',
        'turn_failure'
    )),
    quote_item_id TEXT CHECK (
        quote_item_id IS NULL OR kind IN ('user_message', 'assistant_message')
    ),
    payload TEXT NOT NULL CHECK (
        json_valid(payload)
        AND json_extract(payload, '$.kind') = kind
        AND json_extract(payload, '$.quote_item_id') IS quote_item_id
    ),
    created_at TEXT NOT NULL,
    UNIQUE (user_id, item_id),
    UNIQUE (user_id, message_id),
    FOREIGN KEY (user_id, quote_item_id)
        REFERENCES chat_items(user_id, item_id) ON DELETE CASCADE
);

CREATE INDEX idx_chat_items_user_sequence ON chat_items(user_id, sequence);

CREATE TRIGGER chat_items_append_only
BEFORE UPDATE ON chat_items
BEGIN
    SELECT RAISE(ABORT, 'chat_items are append-only');
END;
