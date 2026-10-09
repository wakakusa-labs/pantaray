/**
 * IPC payload size limits.
 *
 * Centralized so renderer-supplied bounds (ID, display name, path, hostname,
 * array length) are enforced consistently across schemas and assertable from
 * tests. Values target real-world content while guarding against runaway
 * payloads from a compromised renderer.
 */

export const MAX_ID_LENGTH = 128;
export const MAX_DISPLAY_NAME_LENGTH = 200;
export const MAX_PATH_LENGTH = 4096;
export const MAX_HOST_LENGTH = 253;
export const MAX_ARRAY_LENGTH = 1000;
export const MAX_REASON_LENGTH = 1024;
export const MAX_LAST_CHUNK_INDEX = 1_000_000;
export const MAX_ACCELERATOR_LENGTH = 128;
/** A whole conversation copied as Markdown, in UTF-16 code units. */
export const MAX_CLIPBOARD_TEXT_LENGTH = 10_000_000;
