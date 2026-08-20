import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { JsonPipe } from '@angular/common';
import type { AlertDetail } from '../../core/api/operator-api.models';
import { severityLabel, warningLabel } from '../../shared/presentation/incident-labels';

@Component({
  selector: 'app-alert-detail',
  imports: [JsonPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `@if (alert(); as item) {
    <section aria-labelledby="alert-heading">
      <h2 id="alert-heading">Grafana 告警</h2>
      <dl>
        <dt>雲端</dt>
        <dd>{{ item.provider }}</dd>
        <dt>專案／系統代碼</dt>
        <dd>{{ item.folderCode ?? '未提供' }}</dd>
        <dt>告警名稱</dt>
        <dd>{{ item.alertName ?? item.title }}</dd>
        <dt>嚴重度</dt>
        <dd>{{ severity(item.severity) }}</dd>
      </dl>
      <h3>Grafana 告警內容</h3>
      <pre>{{ item.issue.rawText }}</pre>
      @if (item.normalizationWarnings.length) {
        <h3>正規化提醒</h3>
        <ul>
          @for (warning of item.normalizationWarnings; track warning) {
            <li>{{ warningText(warning) }}</li>
          }
        </ul>
      }
      <details>
        <summary>原始 labels</summary>
        <pre>{{ item.labels | json }}</pre>
      </details>
    </section>
  }`,
})
export class AlertDetailComponent {
  readonly alert = input<AlertDetail | null>(null);
  readonly severity = severityLabel;
  readonly warningText = warningLabel;
}
