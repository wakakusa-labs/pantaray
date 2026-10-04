/**
 * A run of consecutive history rows that were last updated on the same local day. `isRecent`
 * marks today and yesterday, whose heading already names the day, so a row needs only its time.
 */
export type HistoryDayGroup<T> = { key: string; label: string; isRecent: boolean; items: T[] };

type DayLabels = { today: string; yesterday: string };

function localDayKey(date: Date): string {
  return `${date.getFullYear()}-${date.getMonth() + 1}-${date.getDate()}`;
}

/**
 * Groups rows under day headings in the order they arrive, so the list keeps the order the
 * backend sent. Days are local to this machine; `now` decides which day is today.
 */
export function groupHistoryByDay<T extends { updated_at: string }>(
  items: readonly T[],
  now: Date,
  labels: DayLabels,
  locale: string
): HistoryDayGroup<T>[] {
  const todayKey = localDayKey(now);
  const yesterdayKey = localDayKey(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1));
  const sameYear = new Intl.DateTimeFormat(locale, { month: 'short', day: 'numeric' });
  const otherYear = new Intl.DateTimeFormat(locale, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  });
  const label = (date: Date, key: string): string => {
    if (key === todayKey) return labels.today;
    if (key === yesterdayKey) return labels.yesterday;
    return (date.getFullYear() === now.getFullYear() ? sameYear : otherYear).format(date);
  };

  const groups: HistoryDayGroup<T>[] = [];
  for (const item of items) {
    const date = new Date(item.updated_at);
    const key = localDayKey(date);
    const last = groups[groups.length - 1];
    if (last?.key === key) {
      last.items.push(item);
    } else {
      groups.push({
        key,
        label: label(date, key),
        isRecent: key === todayKey || key === yesterdayKey,
        items: [item],
      });
    }
  }
  return groups;
}
