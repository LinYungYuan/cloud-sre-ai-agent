import { InjectionToken } from '@angular/core';

export interface RuntimeConfig {
  apiBaseUrl: string;
  locale: 'zh-TW';
  timeZone: 'Asia/Taipei';
}

export const RUNTIME_CONFIG = new InjectionToken<RuntimeConfig>('RUNTIME_CONFIG');

export async function loadRuntimeConfig(fetchConfig: typeof fetch = fetch): Promise<RuntimeConfig> {
  const response = await fetchConfig('/config.json');

  if (!response.ok) {
    throw new Error(`Unable to load runtime configuration (HTTP ${response.status}).`);
  }

  return parseRuntimeConfig(await response.json());
}

export function parseRuntimeConfig(value: unknown): RuntimeConfig {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error('Runtime configuration must be an object.');
  }

  const config = value as Record<string, unknown>;
  const apiBaseUrl = config['apiBaseUrl'];

  if (typeof apiBaseUrl !== 'string' || apiBaseUrl.trim().length === 0) {
    throw new Error('Runtime configuration apiBaseUrl must be a non-empty string.');
  }

  if (config['locale'] !== 'zh-TW') {
    throw new Error('Runtime configuration locale must be zh-TW.');
  }

  if (config['timeZone'] !== 'Asia/Taipei') {
    throw new Error('Runtime configuration timeZone must be Asia/Taipei.');
  }

  return {
    apiBaseUrl,
    locale: 'zh-TW',
    timeZone: 'Asia/Taipei',
  };
}
