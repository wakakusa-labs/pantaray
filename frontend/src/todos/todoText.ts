/** A TODO is one line (the backend refuses more), so the lines of a text are joined. */
export const oneLine = (text: string) => text.replace(/[^\S\n]*\n\s*/g, ' ');
