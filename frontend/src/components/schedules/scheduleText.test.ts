import { afterEach, describe, expect, it, vi } from 'vitest';

import { formatInstant } from './scheduleText';

describe('formatInstant', () => {
  afterEach(() => vi.useRealTimers());

  // Catches a weekday off by the time zone, and a moment in another year read as this one's.
  it('names the weekday, and the year only when it is not this one', () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date(2026, 5, 1));
    const thisYear = new Date(2026, 10, 30, 9, 0).toISOString();
    const nextYear = new Date(2027, 10, 30, 9, 0).toISOString();

    expect(formatInstant(thisYear, 'ja')).toBe('11月30日(月) 9:00');
    expect(formatInstant(nextYear, 'ja')).toBe('2027年11月30日(火) 9:00');
    expect(formatInstant(thisYear, 'en')).toBe('Mon, Nov 30, 9:00 AM');
  });
});
