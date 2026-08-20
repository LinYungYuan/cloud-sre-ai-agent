import { ComponentFixture, TestBed } from '@angular/core/testing';
import { RcaReportComponent } from './rca-report.component';

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
});
