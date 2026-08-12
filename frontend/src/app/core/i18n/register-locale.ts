import { registerLocaleData } from '@angular/common';
import localeZhHant from '@angular/common/locales/zh-Hant';

export function registerZhTwLocale(): void {
  registerLocaleData(localeZhHant, 'zh-TW');
}
