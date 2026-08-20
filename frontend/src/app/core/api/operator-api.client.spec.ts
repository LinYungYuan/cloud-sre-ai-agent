import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { RUNTIME_CONFIG } from '../runtime-config/runtime-config';
import { OperatorApiClient, OperatorApiError } from './operator-api.client';

describe('OperatorApiClient', () => {
  let client: OperatorApiClient;
  let http: HttpTestingController;
  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: RUNTIME_CONFIG,
          useValue: { apiBaseUrl: '/api/v1/', locale: 'zh-TW', timeZone: 'Asia/Taipei' },
        },
      ],
    });
    client = TestBed.inject(OperatorApiClient);
    http = TestBed.inject(HttpTestingController);
  });
  afterEach(() => http.verify());

  it('joins runtime URL and encodes opaque cursor query', () => {
    client.listIncidents('opaque+/=', 25).subscribe((page) => expect(page.items).toEqual([]));
    const request = http.expectOne((item) => item.url === '/api/v1/incidents');
    expect(request.request.params.get('cursor')).toBe('opaque+/=');
    expect(request.request.params.get('limit')).toBe('25');
    request.flush({ items: [], nextCursor: null });
  });

  it('maps RFC 9457 errors without exposing transport internals', () => {
    let captured: unknown;
    client.getIncident('abc').subscribe({ error: (error) => (captured = error) });
    http
      .expectOne('/api/v1/incidents/abc')
      .flush(
        {
          type: 'urn:test',
          title: '找不到資源',
          status: 404,
          code: 'RESOURCE_NOT_FOUND',
          detail: '資源不存在。',
          correlationId: 'c-1',
        },
        { status: 404, statusText: 'Not Found' },
      );
    expect(captured).toBeInstanceOf(OperatorApiError);
    expect((captured as OperatorApiError).problem.correlationId).toBe('c-1');
  });
});
