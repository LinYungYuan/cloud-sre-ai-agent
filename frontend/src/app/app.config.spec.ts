import { LOCALE_ID } from '@angular/core';
import { TestBed } from '@angular/core/testing';

import { createAppConfig } from './app.config';
import { RUNTIME_CONFIG, type RuntimeConfig } from './core/runtime-config/runtime-config';

describe('createAppConfig', () => {
  it('provides the zh-TW locale and validated runtime configuration', () => {
    const runtimeConfig: RuntimeConfig = {
      apiBaseUrl: '/api/v1',
      locale: 'zh-TW',
      timeZone: 'Asia/Taipei',
    };
    const config = createAppConfig(runtimeConfig);

    TestBed.configureTestingModule({ providers: config.providers });

    expect(TestBed.inject(LOCALE_ID)).toBe('zh-TW');
    expect(TestBed.inject(RUNTIME_CONFIG)).toBe(runtimeConfig);
  });
});
