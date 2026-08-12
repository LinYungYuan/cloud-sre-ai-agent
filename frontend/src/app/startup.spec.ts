import { describe, expect, it, vi } from 'vitest';

import { renderFatalStartupMessage, startApplication } from './startup';
import type { RuntimeConfig } from './core/runtime-config/runtime-config';

const runtimeConfig: RuntimeConfig = {
  apiBaseUrl: '/api/v1',
  locale: 'zh-TW',
  timeZone: 'Asia/Taipei',
};

describe('startApplication', () => {
  it('registers locale data and awaits runtime config before bootstrap', async () => {
    const events: string[] = [];
    let resolveConfig!: (config: RuntimeConfig) => void;
    const configPromise = new Promise<RuntimeConfig>((resolve) => {
      resolveConfig = resolve;
    });
    const bootstrap = vi.fn(async (config: RuntimeConfig) => {
      events.push('bootstrap');
      expect(config).toBe(runtimeConfig);
    });
    const startup = startApplication({
      registerLocaleData: () => events.push('locale'),
      loadRuntimeConfig: () => {
        events.push('load-config');
        return configPromise;
      },
      bootstrap,
      renderFatalStartupMessage: vi.fn(),
      reportError: vi.fn(),
    });

    expect(events).toEqual(['locale', 'load-config']);
    expect(bootstrap).not.toHaveBeenCalled();

    resolveConfig(runtimeConfig);
    await startup;

    expect(events).toEqual(['locale', 'load-config', 'bootstrap']);
  });

  it('renders the fatal message and does not bootstrap when config loading fails', async () => {
    const bootstrap = vi.fn();
    const renderFatalMessage = vi.fn();
    const reportError = vi.fn();
    const error = new Error('sensitive config detail');

    await startApplication({
      registerLocaleData: vi.fn(),
      loadRuntimeConfig: vi.fn().mockRejectedValue(error),
      bootstrap,
      renderFatalStartupMessage: renderFatalMessage,
      reportError,
    });

    expect(bootstrap).not.toHaveBeenCalled();
    expect(renderFatalMessage).toHaveBeenCalledOnce();
    expect(renderFatalMessage).toHaveBeenCalledWith();
    expect(reportError).toHaveBeenCalledWith(error);
  });

  it('renders the fatal message when Angular bootstrap fails', async () => {
    const renderFatalMessage = vi.fn();

    await startApplication({
      registerLocaleData: vi.fn(),
      loadRuntimeConfig: vi.fn().mockResolvedValue(runtimeConfig),
      bootstrap: vi.fn().mockRejectedValue(new Error('bootstrap failed')),
      renderFatalStartupMessage: renderFatalMessage,
      reportError: vi.fn(),
    });

    expect(renderFatalMessage).toHaveBeenCalledOnce();
  });
});

describe('renderFatalStartupMessage', () => {
  it('replaces the blank shell with an accessible Traditional Chinese message', () => {
    document.body.replaceChildren(document.createElement('app-root'));

    renderFatalStartupMessage(document);

    const alert = document.body.querySelector('[role="alert"]');
    expect(alert).toBeTruthy();
    expect(alert?.textContent).toContain('應用程式無法啟動');
    expect(alert?.textContent).toContain('請稍後重新整理頁面');
    expect(document.body.querySelector('app-root')).toBeNull();
  });
});
