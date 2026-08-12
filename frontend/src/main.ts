import { registerLocaleData } from '@angular/common';
import localeZhHant from '@angular/common/locales/zh-Hant';
import { bootstrapApplication } from '@angular/platform-browser';
import { AppComponent } from './app/app.component';
import { createAppConfig } from './app/app.config';
import { loadRuntimeConfig } from './app/core/runtime-config/runtime-config';

async function startApplication(): Promise<void> {
  registerLocaleData(localeZhHant);

  const runtimeConfig = await loadRuntimeConfig();

  await bootstrapApplication(AppComponent, createAppConfig(runtimeConfig));
}

void startApplication().catch((error: unknown) => console.error(error));
