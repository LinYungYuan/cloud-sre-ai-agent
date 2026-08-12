import { describe, expect, it, vi } from 'vitest';

import { loadRuntimeConfig, parseRuntimeConfig } from './runtime-config';

describe('parseRuntimeConfig', () => {
  const validConfig = {
    apiBaseUrl: '/api/v1',
    locale: 'zh-TW',
    timeZone: 'Asia/Taipei',
  };

  it('returns a valid runtime configuration', () => {
    expect(parseRuntimeConfig(validConfig)).toEqual(validConfig);
  });

  it('rejects a non-object value', () => {
    expect(() => parseRuntimeConfig(null)).toThrowError(/object/);
  });

  it('rejects a missing apiBaseUrl', () => {
    const { apiBaseUrl: _apiBaseUrl, ...withoutApiBaseUrl } = validConfig;

    expect(() => parseRuntimeConfig(withoutApiBaseUrl)).toThrowError(/apiBaseUrl/);
  });

  it('rejects an empty apiBaseUrl', () => {
    expect(() => parseRuntimeConfig({ ...validConfig, apiBaseUrl: '  ' })).toThrowError(
      /apiBaseUrl/,
    );
  });

  it('rejects a locale other than zh-TW', () => {
    expect(() =>
      parseRuntimeConfig({
        apiBaseUrl: '/api/v1',
        locale: 'en-US',
        timeZone: 'Asia/Taipei',
      }),
    ).toThrowError(/zh-TW/);
  });

  it('rejects a timeZone other than Asia/Taipei', () => {
    expect(() => parseRuntimeConfig({ ...validConfig, timeZone: 'UTC' })).toThrowError(
      /Asia\/Taipei/,
    );
  });

  it('rejects an unknown property', () => {
    expect(() => parseRuntimeConfig({ ...validConfig, unexpected: true })).toThrowError(
      /unexpected/,
    );
  });

  it('rejects the unsupported sseUrl property', () => {
    expect(() => parseRuntimeConfig({ ...validConfig, sseUrl: '/events' })).toThrowError(/sseUrl/);
  });
});

describe('loadRuntimeConfig', () => {
  it('loads and validates config.json', async () => {
    const value = {
      apiBaseUrl: '/api/v1',
      locale: 'zh-TW',
      timeZone: 'Asia/Taipei',
    };
    const fetchConfig = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(value), {
        headers: { 'Content-Type': 'application/json' },
        status: 200,
      }),
    );

    await expect(loadRuntimeConfig(fetchConfig)).resolves.toEqual(value);
    expect(fetchConfig).toHaveBeenCalledOnce();
    expect(fetchConfig).toHaveBeenCalledWith('/config.json');
  });

  it('rejects an unsuccessful config response', async () => {
    const fetchConfig = vi
      .fn<typeof fetch>()
      .mockResolvedValue(new Response(null, { status: 503 }));

    await expect(loadRuntimeConfig(fetchConfig)).rejects.toThrowError(/503/);
  });
});
