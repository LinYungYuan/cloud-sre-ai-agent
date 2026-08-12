import { formatDate, getLocaleId } from '@angular/common';
import { describe, expect, it } from 'vitest';

import { registerZhTwLocale } from './register-locale';

describe('registerZhTwLocale', () => {
  it('registers Traditional Chinese locale data under the zh-TW application locale', () => {
    registerZhTwLocale();

    expect(getLocaleId('zh-TW')).toBe('zh-Hant');
    expect(formatDate('2026-08-12T00:00:00Z', 'longDate', 'zh-TW', 'UTC')).toBe(
      '2026年8月12日',
    );
  });
});
