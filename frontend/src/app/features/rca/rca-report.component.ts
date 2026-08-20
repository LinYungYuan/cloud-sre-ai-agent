import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import type { RcaReport, RcaRun } from '../../core/api/operator-api.models';
import { rcaStatusLabel } from '../../shared/presentation/incident-labels';

@Component({
  selector: 'app-rca-report',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<section aria-labelledby="rca-heading">
    <h2 id="rca-heading">根因分析（RCA）</h2>
    @if (run(); as current) {
      <p>狀態：{{ status(current.status) }}</p>
    }
    @if (report(); as item) {
      <p>{{ item.summary }}</p>
      <h3>根因／主要假設</h3>
      <p>{{ item.rootCause }}</p>
      @if (item.confidence !== null) {
        <p>信心程度：{{ confidencePercent(item.confidence) }}%</p>
      }
      <h3>影響</h3>
      <p>{{ item.impact }}</p>
      <h3>修復建議（需人工審查）</h3>
      <ul>
        @for (recommendation of item.recommendations; track recommendation) {
          <li>{{ recommendation }}</li>
        }
      </ul>
    } @else {
      <p>報告尚未產生；請稍後手動重新整理。</p>
    }
  </section>`,
})
export class RcaReportComponent {
  readonly run = input<RcaRun | null>(null);
  readonly report = input<RcaReport | null>(null);
  readonly status = rcaStatusLabel;
  readonly confidencePercent = (value: number): number => Math.round(value * 100);
}
