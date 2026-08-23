import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { RUNTIME_CONFIG } from '../runtime-config/runtime-config';
import { OperatorApiClient, OperatorApiError } from './operator-api.client';

const traceFixture = {
  schemaVersion: 1,
  traceId: 'trace-1',
  rootServiceName: 'checkout-api',
  rootOperationName: 'POST /checkout',
  startedAt: '2026-08-13T06:30:00Z',
  durationMs: 1925,
  spanCount: 5,
  representativeScore: 0.96,
  truncated: false,
  spans: [
    {
      spanId: 'root',
      parentSpanId: null,
      serviceName: 'checkout-api',
      operationName: 'POST /checkout',
      startOffsetMs: 0,
      durationMs: 1925,
      status: 'ERROR',
      kind: 'SERVER',
      criticalPath: true,
      attributes: { 'http.response.status_code': 500 },
    },
    {
      spanId: 'inventory-client',
      parentSpanId: 'root',
      serviceName: 'checkout-api',
      operationName: 'inventory.reserve',
      startOffsetMs: 20,
      durationMs: 1810,
      status: 'ERROR',
      kind: 'CLIENT',
      criticalPath: true,
      attributes: { 'rpc.system': 'grpc' },
    },
    {
      spanId: 'inventory-server',
      parentSpanId: 'inventory-client',
      serviceName: 'inventory-service',
      operationName: 'inventory.reserve',
      startOffsetMs: 35,
      durationMs: 1760,
      status: 'ERROR',
      kind: 'SERVER',
      criticalPath: true,
      attributes: { 'rpc.service': 'inventory' },
    },
    {
      spanId: 'db',
      parentSpanId: 'inventory-server',
      serviceName: 'inventory-service',
      operationName: 'db.connection.acquire',
      startOffsetMs: 320,
      durationMs: 1480,
      status: 'ERROR',
      kind: 'INTERNAL',
      criticalPath: true,
      attributes: { 'db.system': 'postgresql' },
    },
    {
      spanId: 'cache',
      parentSpanId: 'inventory-server',
      serviceName: 'inventory-service',
      operationName: 'cache.lookup',
      startOffsetMs: 75,
      durationMs: 120,
      status: 'OK',
      kind: 'CLIENT',
      criticalPath: false,
      attributes: { 'server.port': 6379 },
    },
  ],
};

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

  it('loads the encoded trace waterfall resource', () => {
    client.getTraceWaterfall('run/1').subscribe((value) =>
      expect(value.trace?.traceId).toBe('trace-1'),
    );
    const request = http.expectOne('/api/v1/rca-runs/run%2F1/trace-waterfall');
    request.flush({ trace: traceFixture });
  });
});
