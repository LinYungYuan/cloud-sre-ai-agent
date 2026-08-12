import {
  type ApplicationConfig,
  LOCALE_ID,
  provideBrowserGlobalErrorListeners,
} from '@angular/core';
import { provideRouter } from '@angular/router';

import { routes } from './app.routes';
import { RUNTIME_CONFIG, type RuntimeConfig } from './core/runtime-config/runtime-config';

export function createAppConfig(runtimeConfig: RuntimeConfig): ApplicationConfig {
  return {
    providers: [
      provideBrowserGlobalErrorListeners(),
      provideRouter(routes),
      { provide: LOCALE_ID, useValue: 'zh-TW' },
      { provide: RUNTIME_CONFIG, useValue: runtimeConfig },
    ],
  };
}
