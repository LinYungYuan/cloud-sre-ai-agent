import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { registerZhTwLocale } from '../../core/i18n/register-locale';
import { RUNTIME_CONFIG } from '../../core/runtime-config/runtime-config';
import { IncidentDetailComponent } from './incident-detail.component';

const incidentId = 'd0000000-0000-4000-8000-000000000001';
const runId = 'e0000000-0000-4000-8000-000000000001';
const timestamp = '2026-08-23T03:00:00.000Z';

const incidentFixture = {
  id: incidentId,
  incidentNumber: 'INC-001',
  title: '結帳 API 延遲',
  severity: 'SEV1' as const,
  status: 'INVESTIGATING' as const,
  alertState: 'FIRING' as const,
  rcaStatus: 'SUCCEEDED' as const,
  provider: 'GCP' as const,
  folderCode: 'checkout',
  alertName: 'checkout-latency',
  scope: { teamId: 'team-1', projectId: 'project-1', environmentId: 'prod', serviceId: 'checkout' },
  acknowledged: false,
  acknowledgedAt: null,
  acknowledgedBy: null,
  assignee: null,
  openedAt: timestamp,
  updatedAt: timestamp,
  resolvedAt: null,
  version: 1,
  description: '結帳服務延遲異常',
  alertIds: [],
  rcaRunIds: [runId],
};

const runFixture = {
  id: runId,
  incidentId,
  runNumber: 1,
  status: 'SUCCEEDED' as const,
  createdAt: timestamp,
  updatedAt: timestamp,
  startedAt: timestamp,
  completedAt: timestamp,
  failureCode: null,
  reportId: 'f0000000-0000-4000-8000-000000000001',
};

const reportFixture = {
  id: 'f0000000-0000-4000-8000-000000000001',
  rcaRunId: runId,
  incidentId,
  reportVersion: 1,
  status: 'SUCCEEDED',
  summary: '資料庫連線池已耗盡。',
  rootCause: '資料庫連線池耗盡',
  confidence: 0.91,
  impact: '結帳請求逾時',
  recommendations: ['擴充連線池'],
  hypotheses: [],
  claims: [],
  createdAt: timestamp,
};

const traceFixture = {
  schemaVersion: 1 as const,
  traceId: 'trace-1',
  rootServiceName: 'checkout-api',
  rootOperationName: 'POST /checkout',
  startedAt: timestamp,
  durationMs: 1480,
  spanCount: 5,
  representativeScore: 0.92,
  truncated: false,
  spans: Array.from({ length: 5 }, (_, index) => ({
    spanId: `span-${index + 1}`,
    parentSpanId: index === 0 ? null : `span-${index}`,
    serviceName: index === 0 ? 'checkout-api' : 'inventory-service',
    operationName: `operation-${index + 1}`,
    startOffsetMs: index * 100,
    durationMs: 500,
    status: index === 3 ? ('ERROR' as const) : ('OK' as const),
    kind: index === 0 ? ('SERVER' as const) : ('CLIENT' as const),
    criticalPath: true,
    attributes: {},
  })),
};

describe('IncidentDetailComponent', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    registerZhTwLocale();
    await TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([{ path: 'incidents/:id', component: IncidentDetailComponent }]),
        {
          provide: RUNTIME_CONFIG,
          useValue: { apiBaseUrl: '/api/v1', locale: 'zh-TW', timeZone: 'Asia/Taipei' },
        },
      ],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('keeps the RCA report visible when Trace fails and retries only Trace', async () => {
    const harness = await RouterTestingHarness.create();
    await harness.navigateByUrl(`/incidents/${incidentId}`, IncidentDetailComponent);
    http.expectOne(`/api/v1/incidents/${incidentId}`).flush(incidentFixture);
    http
      .expectOne((request) => request.url === `/api/v1/incidents/${incidentId}/rca-runs`)
      .flush({ items: [runFixture], nextCursor: null });
    http.expectOne(`/api/v1/rca-runs/${runId}/report`).flush(reportFixture);
    harness.detectChanges();
    const traceRequest = http.expectOne(`/api/v1/rca-runs/${runId}/trace-waterfall`);
    traceRequest.flush(
      { title: 'Unavailable', detail: 'Trace backend unavailable' },
      { status: 503, statusText: 'Service Unavailable' },
    );
    harness.detectChanges();

    expect(harness.routeNativeElement?.textContent).toContain('資料庫連線池耗盡');
    const retry = harness.routeNativeElement?.querySelector(
      '[data-trace-retry]',
    ) as HTMLButtonElement;
    expect(retry).not.toBeNull();
    retry.click();
    harness.detectChanges();

    http.expectNone(`/api/v1/incidents/${incidentId}`);
    http.expectNone((request) => request.url === `/api/v1/incidents/${incidentId}/rca-runs`);
    http.expectNone(`/api/v1/rca-runs/${runId}/report`);
    http.expectOne(`/api/v1/rca-runs/${runId}/trace-waterfall`).flush({ trace: traceFixture });
    harness.detectChanges();

    expect(harness.routeNativeElement?.querySelectorAll('[data-span-id]').length).toBe(5);
  });
});
