import { describe, expect, it } from 'vitest';

import { groupHistoryByDay } from './historyDayGroups';

const LABELS = { today: 'Today', yesterday: 'Yesterday' };
// Local times, so the expectations hold in any time zone.
const at = (year: number, month: number, day: number, hour: number) => ({
  updated_at: new Date(year, month - 1, day, hour).toISOString(),
});

const summarize = (groups: ReturnType<typeof groupHistoryByDay>) =>
  groups.map((group) => [group.label, group.items.length, group.isRecent]);

describe('groupHistoryByDay', () => {
  const now = new Date(2026, 9, 2, 9);

  it('names today and yesterday by local day, then dates, keeping the list order', () => {
    const items = [
      at(2026, 10, 2, 8),
      at(2026, 10, 2, 0),
      at(2026, 10, 1, 23),
      at(2026, 10, 1, 1),
      at(2026, 9, 30, 22),
    ];

    expect(summarize(groupHistoryByDay(items, now, LABELS, 'en-US'))).toEqual([
      ['Today', 2, true],
      ['Yesterday', 2, true],
      ['Sep 30', 1, false],
    ]);
    expect(groupHistoryByDay([at(2026, 9, 30, 22)], now, LABELS, 'ja-JP')[0].label).toBe('9月30日');
  });

  it('adds the year to days from another year, across the year boundary too', () => {
    const newYear = new Date(2027, 0, 1, 9);
    const items = [at(2026, 12, 31, 23), at(2026, 12, 30, 12)];

    expect(summarize(groupHistoryByDay(items, newYear, LABELS, 'en-US'))).toEqual([
      ['Yesterday', 1, true],
      ['Dec 30, 2026', 1, false],
    ]);
  });
});
