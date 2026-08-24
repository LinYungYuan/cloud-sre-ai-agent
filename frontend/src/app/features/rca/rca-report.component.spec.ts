import { ComponentFixture, TestBed } from '@angular/core/testing';
import type { TraceWaterfall } from '../../core/api/operator-api.models';
import { RcaReportComponent } from './rca-report.component';

const traceFixture: TraceWaterfall = {
  schemaVersion: 1,
  traceId: 'trace-1',
  rootServiceName: 'checkout-api',
  rootOperationName: 'POST /checkout',
  startedAt: '2026-08-23T03:00:00.000Z',
  durationMs: 1480,
  spanCount: 1,
  representativeScore: 0.92,
  truncated: false,
  spans: [
    {
      spanId: 'checkout',
      parentSpanId: null,
      serviceName: 'checkout-api',
      operationName: 'POST /checkout',
      startOffsetMs: 0,
      durationMs: 1480,
      status: 'OK',
      kind: 'SERVER',
      criticalPath: true,
      attributes: {},
    },
  ],
};

describe('RcaReportComponent', () => {
  let fixture: ComponentFixture<RcaReportComponent>;
  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [RcaReportComponent] }).compileComponents();
    fixture = TestBed.createComponent(RcaReportComponent);
  });
  it('labels remediation as human-reviewed and keeps partial explanation', () => {
    fixture.componentRef.setInput('report', {
      id: 'r',
      rcaRunId: 'run',
      incidentId: 'i',
      reportVersion: 1,
      status: 'PARTIAL',
      summary: '證據不足，目前沒有 AWS MCP 證據。',
      rootCause: '尚待確認',
      confidence: 0.88,
      impact: '未知',
      recommendations: ['確認監控資料'],
      hypotheses: [],
      claims: [],
      createdAt: '2026-08-13T06:30:00Z',
    });
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('證據不足');
    expect(fixture.nativeElement.textContent).toContain('88%');
    expect(fixture.nativeElement.textContent).toContain('修復建議（需人工審查）');
  });

  it('separates impact signals in Metrics, Logs, Traces order and nests the waterfall', () => {
    fixture.componentRef.setInput('report', {
      id: 'r',
      rcaRunId: 'run',
      incidentId: 'i',
      reportVersion: 1,
      status: 'SUCCEEDED',
      summary: '摘要',
      rootCause: '連線池耗盡',
      confidence: 0.88,
      impact:
        'Logs：843 筆 DB_POOL_EXHAUSTED；Metrics：p95 1840ms、5xx 12.7%；Traces：db.connection.acquire 1480ms',
      recommendations: ['擴充連線池'],
      hypotheses: [],
      claims: [],
      createdAt: '2026-08-13T06:30:00Z',
    });
    fixture.componentRef.setInput('traceState', { status: 'ready', trace: traceFixture });
    fixture.detectChanges();

    const signalSections = Array.from(
      fixture.nativeElement.querySelectorAll('[data-impact-signal]') as NodeListOf<HTMLElement>,
    );
    expect(signalSections.map((section) => section.dataset['impactSignal'])).toEqual([
      'metrics',
      'logs',
      'traces',
    ]);
    expect(signalSections[0].textContent).toContain('p95 1840ms、5xx 12.7%');
    expect(signalSections[1].textContent).toContain('843 筆 DB_POOL_EXHAUSTED');
    expect(signalSections[2].textContent).toContain('db.connection.acquire 1480ms');
    expect(signalSections[2].querySelector('app-trace-waterfall')).not.toBeNull();

    const headings = Array.from(
      fixture.nativeElement.querySelectorAll('h3, h4, h5') as NodeListOf<HTMLHeadingElement>,
    ).map((heading) => `${heading.tagName}:${heading.textContent?.trim()}`);
    expect(headings).toEqual([
      'H3:根因／主要假設',
      'H3:影響',
      'H4:Metrics',
      'H4:Logs',
      'H4:Traces',
      'H5:Trace 瀑布圖',
      'H3:修復建議（需人工審查）',
    ]);
  });

  it('keeps an unstructured legacy impact without dropping the trace waterfall', () => {
    fixture.componentRef.setInput('report', {
      id: 'r',
      rcaRunId: 'run',
      incidentId: 'i',
      reportVersion: 1,
      status: 'PARTIAL',
      summary: '摘要',
      rootCause: '尚待確認',
      confidence: null,
      impact: '目前資料不足，影響範圍尚待確認。',
      recommendations: [],
      hypotheses: [],
      claims: [],
      createdAt: '2026-08-13T06:30:00Z',
    });
    fixture.componentRef.setInput('traceState', { status: 'empty' });
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('[data-impact-fallback]').textContent).toContain(
      '目前資料不足，影響範圍尚待確認。',
    );
    const traces = fixture.nativeElement.querySelector('[data-impact-signal="traces"]');
    expect(traces).not.toBeNull();
    expect(traces.querySelector('app-trace-waterfall')).not.toBeNull();
  });

  it('emits retryTrace when the embedded waterfall retry is clicked', () => {
    let retries = 0;
    fixture.componentInstance.retryTrace.subscribe(() => retries++);
    fixture.componentRef.setInput('report', {
      id: 'r',
      rcaRunId: 'run',
      incidentId: 'i',
      reportVersion: 1,
      status: 'SUCCEEDED',
      summary: '摘要',
      rootCause: '連線池耗盡',
      confidence: null,
      impact: '結帳失敗',
      recommendations: [],
      hypotheses: [],
      claims: [],
      createdAt: '2026-08-13T06:30:00Z',
    });
    fixture.componentRef.setInput('traceState', { status: 'error' });
    fixture.detectChanges();

    (fixture.nativeElement.querySelector('[data-trace-retry]') as HTMLButtonElement).click();

    expect(retries).toBe(1);
  });
});
