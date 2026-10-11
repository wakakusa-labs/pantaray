/** `pantaray-file:///` links: how a reply names a local file the user can open. */

export function localPathFromPantarayFileHref(href: string): string | null {
  if (!href.startsWith('pantaray-file:///')) return null;
  try {
    const url = new URL(href);
    return decodeURIComponent(url.pathname);
  } catch {
    return null;
  }
}

// A reply often names a file as a bare pantaray-file:/// URL rather than a Markdown
// link, and GFM links only http(s) and www on its own. Japanese prose runs on with
// no space, so the URL ends at whitespace, a quote, a Japanese stop, comma or quote
// bracket, or a closing bracket it did not open, and drops trailing punctuation.
export const BARE_FILE_URL_START = 'pantaray-file:///';
const URL_STOPS = new Set([...'<>"“”。、「」『』']);
const CLOSING_BRACKETS: Record<string, string> = { ')': '(', '）': '（', ']': '[', '】': '【' };
const OPENING_BRACKETS = new Set(Object.values(CLOSING_BRACKETS));
const TRAILING_PUNCTUATION = /[.,:;!?']+$/u;

export function bareFileUrlAt(text: string, start: number): string {
  const depth = new Map<string, number>();
  let end = start + BARE_FILE_URL_START.length;
  for (; end < text.length; end += 1) {
    const char = text[end];
    if (/\s/u.test(char) || URL_STOPS.has(char)) break;
    if (OPENING_BRACKETS.has(char)) depth.set(char, (depth.get(char) ?? 0) + 1);
    const opening = CLOSING_BRACKETS[char];
    if (opening !== undefined) {
      const open = depth.get(opening) ?? 0;
      if (open === 0) break;
      depth.set(opening, open - 1);
    }
  }
  return text.slice(start, end).replace(TRAILING_PUNCTUATION, '');
}

/** Every local path a text links with `pantaray-file:///`, bare or as a Markdown link. */
export function pantarayFilePaths(text: string): string[] {
  const paths: string[] = [];
  for (
    let start = text.indexOf(BARE_FILE_URL_START);
    start !== -1;
    start = text.indexOf(BARE_FILE_URL_START, start + BARE_FILE_URL_START.length)
  ) {
    const localPath = localPathFromPantarayFileHref(bareFileUrlAt(text, start));
    if (localPath) paths.push(localPath);
  }
  return paths;
}
