import { ComponentFixture, TestBed } from '@angular/core/testing';
import { AlertDetailComponent } from './alert-detail.component';

describe('AlertDetailComponent', () => {
  let fixture: ComponentFixture<AlertDetailComponent>;
  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [AlertDetailComponent] }).compileComponents();
    fixture = TestBed.createComponent(AlertDetailComponent);
  });
  it('shows AlertValues unchanged as Grafana alert content', () => {
    const rawText = 'Account: 123456789012\nDB Name: production-rds-01\nValue: 85.23%\n<br>';
    fixture.componentRef.setInput('alert', {
      id: 'a',
      sourceId: 's',
      incidentId: 'i',
      fingerprint: 'f',
      title: 'High CPU',
      severity: 'SEV1',
      state: 'FIRING',
      classificationStatus: 'CLASSIFIED',
      scope: null,
      startsAt: '2026-08-13T06:30:00Z',
      endsAt: null,
      updatedAt: '2026-08-13T06:30:00Z',
      provider: 'AWS',
      folderCode: 'COM-LX-BOA-01',
      alertName: 'High CPU',
      severityRaw: 'ERROR',
      issue: {
        rawText,
        source: 'grafana.annotations.AlertValues',
        contentType: 'text/plain',
        untrusted: true,
      },
      normalization: null,
      normalizationWarnings: ['resource_unclassified'],
      labels: {},
      annotations: {},
      generatorUrl: null,
    });
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('Grafana 告警內容');
    expect(fixture.nativeElement.textContent).toContain(rawText);
    expect(fixture.nativeElement.textContent).toContain('資源未分類');
  });
});
