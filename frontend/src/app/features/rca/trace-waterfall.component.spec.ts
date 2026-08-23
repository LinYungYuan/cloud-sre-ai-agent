import { ComponentFixture, TestBed } from '@angular/core/testing';
import type { TraceWaterfall } from '../../core/api/operator-api.models';
import { TraceWaterfallComponent } from './trace-waterfall.component';

const traceFixture: TraceWaterfall = {
  schemaVersion: 1,
  traceId: 'e8c0f1d2a3b4c5d6',
  rootServiceName: 'checkout-api',
  rootOperationName: 'POST /checkout',
  startedAt: '2026-08-23T03:00:00.000Z',
  durationMs: 1480,
  spanCount: 5,
  representativeScore: 0.92,
  truncated: false,
  spans: [
    {
      spanId: 'checkout', parentSpanId: null, serviceName: 'checkout-api', operationName: 'POST /checkout',
      startOffsetMs: 0, durationMs: 1480, status: 'OK', kind: 'SERVER', criticalPath: true, attributes: {},
    },
    {
      spanId: 'reserve-items', parentSpanId: 'checkout', serviceName: 'inventory-service', operationName: 'reserve-items',
      startOffsetMs: 120, durationMs: 1100, status: 'OK', kind: 'CLIENT', criticalPath: true, attributes: { 'http.status_code': 503 },
    },
    {
      spanId: 'inventory-query', parentSpanId: 'reserve-items', serviceName: 'inventory-service', operationName: 'SELECT inventory',
      startOffsetMs: 180, durationMs: 900, status: 'OK', kind: 'INTERNAL', criticalPath: true, attributes: {},
    },
    {
      spanId: 'db-connection', parentSpanId: 'inventory-query', serviceName: 'postgres-primary', operationName: 'db.connection.acquire',
      startOffsetMs: 240, durationMs: 760, status: 'ERROR', kind: 'CLIENT', criticalPath: true, attributes: { 'db.system': 'postgresql' },
    },
    {
      spanId: 'audit', parentSpanId: 'checkout', serviceName: 'audit-service', operationName: 'write-audit-event',
      startOffsetMs: 420, durationMs: 90, status: 'OK', kind: 'PRODUCER', criticalPath: false, attributes: {},
    },
  ],
};

describe('TraceWaterfallComponent', () => {
  let fixture: ComponentFixture<TraceWaterfallComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [TraceWaterfallComponent] }).compileComponents();
    fixture = TestBed.createComponent(TraceWaterfallComponent);
  });

  it('renders a nested five-span timeline with the critical error selected', () => {
    fixture.componentRef.setInput('state', { status: 'ready', trace: traceFixture });
    fixture.detectChanges();

    const rows = fixture.nativeElement.querySelectorAll('[data-span-id]');
    expect(rows.length).toBe(5);
    expect(rows[3].getAttribute('aria-selected')).toBe('true');
    expect(rows[3].textContent).toContain('db.connection.acquire');
    expect(fixture.nativeElement.textContent).toContain('1,480ms');
    expect(fixture.nativeElement.textContent).toContain('Critical path');
  });

  it('shows the selected span details after clicking a row', () => {
    fixture.componentRef.setInput('state', { status: 'ready', trace: traceFixture });
    fixture.detectChanges();

    const row = fixture.nativeElement.querySelector('[data-span-id="reserve-items"]') as HTMLButtonElement;
    row.click();
    fixture.detectChanges();

    const details = fixture.nativeElement.querySelector('[data-trace-details]');
    expect(details.textContent).toContain('reserve-items');
    expect(details.textContent).toContain('inventory-service');
  });

  it('selects a focused row with Enter', () => {
    fixture.componentRef.setInput('state', { status: 'ready', trace: traceFixture });
    fixture.detectChanges();

    const row = fixture.nativeElement.querySelector('[data-span-id="reserve-items"]') as HTMLButtonElement;
    row.focus();
    row.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    fixture.detectChanges();

    expect(row.getAttribute('aria-selected')).toBe('true');
    expect(fixture.nativeElement.querySelector('[data-trace-details]').textContent).toContain('reserve-items');
  });

  it('shows a loading skeleton while the trace is loading', () => {
    fixture.componentRef.setInput('state', { status: 'loading' });
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('[data-trace-loading]')).not.toBeNull();
  });

  it('explains when no trace is available', () => {
    fixture.componentRef.setInput('state', { status: 'empty' });
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('目前沒有可顯示的 Trace 資料');
  });

  it('emits retry when trace loading fails', () => {
    let retries = 0;
    fixture.componentInstance.retry.subscribe(() => retries++);
    fixture.componentRef.setInput('state', { status: 'error' });
    fixture.detectChanges();

    const retry = fixture.nativeElement.querySelector('[data-trace-retry]') as HTMLButtonElement;
    expect(fixture.nativeElement.textContent).toContain('Trace 資料載入失敗');
    retry.click();

    expect(retries).toBe(1);
  });

  it('notifies the user when the stored trace was truncated', () => {
    fixture.componentRef.setInput('state', { status: 'ready', trace: { ...traceFixture, truncated: true } });
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('僅顯示前 100 個關鍵 Span');
  });
});
