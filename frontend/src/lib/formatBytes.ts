const BYTE_UNITS = ['B', 'KB', 'MB'] as const;
const BYTE_UNIT_STEP = 1024;
const numberFormat = new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 });

/**
 * A byte count as the user reads it: "512 B", "1.2 KB", "20 MB". Binary steps, so the 20 MiB
 * document limit reads as the "20 MB" the product copy states. The same in both UI languages.
 */
export function formatBytes(bytes: number): string {
  let value = bytes;
  let unit = 0;
  while (value >= BYTE_UNIT_STEP && unit < BYTE_UNITS.length - 1) {
    value /= BYTE_UNIT_STEP;
    unit += 1;
  }
  return `${numberFormat.format(value)} ${BYTE_UNITS[unit]}`;
}
