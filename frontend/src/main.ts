import { registerLocaleData } from '@angular/common';
import localeZhHant from '@angular/common/locales/zh-Hant';
import { bootstrapApplication } from '@angular/platform-browser';
import { AppComponent } from './app/app.component';
import { createAppConfig } from './app/app.config';
import { loadRuntimeConfig } from './app/core/runtime-config/runtime-config';
import { renderFatalStartupMessage, startApplication } from './app/startup';

void startApplication({
  registerLocaleData: () => registerLocaleData(localeZhHant),
  loadRuntimeConfig,
  bootstrap: async (runtimeConfig) => {
    await bootstrapApplication(AppComponent, createAppConfig(runtimeConfig));
  },
  renderFatalStartupMessage: () => renderFatalStartupMessage(document),
  reportError: (error) => console.error('Application startup failed.', error),
});
