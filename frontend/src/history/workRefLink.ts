// The link format is shared with the main process, which opens a link clicked in another app.
export {
  findWorkRefLinks,
  workRefLink,
  type WorkRefKind,
  type WorkRefLink,
} from '../../electron/src/history/workRefLink';

// One line of a chip; a title is a request or a suggestion's whole text.
const CHIP_TITLE_MAX_CODE_POINTS = 50;

/** What a chip shows of a title: its first line, cut to fit one line of the composer. */
export function chipTitle(title: string): string {
  const line = title
    .split('\n')
    .map((part) => part.trim())
    .find((part) => part !== '');
  const codePoints = Array.from(line ?? title.trim());
  return codePoints.length <= CHIP_TITLE_MAX_CODE_POINTS
    ? codePoints.join('')
    : `${codePoints.slice(0, CHIP_TITLE_MAX_CODE_POINTS - 1).join('')}…`;
}
