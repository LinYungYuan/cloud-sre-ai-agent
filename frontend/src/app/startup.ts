import type { RuntimeConfig } from './core/runtime-config/runtime-config';

export interface StartupDependencies {
  registerLocaleData: () => void;
  loadRuntimeConfig: () => Promise<RuntimeConfig>;
  bootstrap: (runtimeConfig: RuntimeConfig) => Promise<void>;
  renderFatalStartupMessage: () => void;
  reportError: (error: unknown) => void;
}

export async function startApplication(dependencies: StartupDependencies): Promise<void> {
  try {
    dependencies.registerLocaleData();
    const runtimeConfig = await dependencies.loadRuntimeConfig();
    await dependencies.bootstrap(runtimeConfig);
  } catch (error: unknown) {
    try {
      dependencies.reportError(error);
    } finally {
      dependencies.renderFatalStartupMessage();
    }
  }
}

export function renderFatalStartupMessage(documentRef: Document): void {
  const message = documentRef.createElement('main');
  message.className = 'fatal-startup';
  message.setAttribute('role', 'alert');
  message.setAttribute('aria-labelledby', 'fatal-startup-title');

  const title = documentRef.createElement('h1');
  title.id = 'fatal-startup-title';
  title.textContent = '應用程式無法啟動';

  const guidance = documentRef.createElement('p');
  guidance.textContent = '目前無法載入必要的系統設定，請稍後重新整理頁面或聯絡系統管理員。';

  message.append(title, guidance);
  documentRef.body.replaceChildren(message);
}
