import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import type {
  RcaReport,
  RcaRun,
  TraceWaterfallLoadState,
} from '../../core/api/operator-api.models';
import { rcaStatusLabel } from '../../shared/presentation/incident-labels';
import { TraceWaterfallComponent } from './trace-waterfall.component';

interface ImpactSignals {
  metrics: string | null;
  logs: string | null;
  traces: string | null;
  fallback: string | null;
  hasSignals: boolean;
}

@Component({
  selector: 'app-rca-report',
  imports: [TraceWaterfallComponent],
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
      <h3 id="impact-heading">影響</h3>
      @let impact = impactSignals(item.impact);
      @if (impact.fallback) {
        <p data-impact-fallback>{{ impact.fallback }}</p>
      }
      @if (impact.hasSignals) {
        <div class="impact-signals" aria-labelledby="impact-heading">
          <section class="impact-signal metrics" data-impact-signal="metrics">
            <h4>Metrics</h4>
            <p>{{ impact.metrics ?? '未提供獨立 Metrics 摘要。' }}</p>
          </section>
          <section class="impact-signal logs" data-impact-signal="logs">
            <h4>Logs</h4>
            <p>{{ impact.logs ?? '未提供獨立 Logs 摘要。' }}</p>
          </section>
          <section class="impact-signal traces" data-impact-signal="traces">
            <h4>Traces</h4>
            @if (impact.traces) {
              <p>{{ impact.traces }}</p>
            }
            <app-trace-waterfall [state]="traceState()" (retry)="retryTrace.emit()" />
          </section>
        </div>
      } @else {
        <section class="impact-signal traces" data-impact-signal="traces">
          <h4>Traces</h4>
          <app-trace-waterfall [state]="traceState()" (retry)="retryTrace.emit()" />
        </section>
      }
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
  styles: [
    `
      .impact-signals {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
        gap: 14px;
        margin: 12px 0 22px;
      }
      .impact-signal {
        border: 1px solid #dbe3ef;
        border-left: 4px solid #64748b;
        border-radius: 10px;
        padding: 14px 16px;
        background: #f8fafc;
      }
      .impact-signal.metrics {
        border-left-color: #0284c7;
        background: #f0f9ff;
      }
      .impact-signal.logs {
        border-left-color: #d97706;
        background: #fffbeb;
      }
      .impact-signal.traces {
        grid-column: 1 / -1;
        border-left-color: #7c3aed;
        background: #faf5ff;
        margin-bottom: 22px;
      }
      .impact-signals .impact-signal.traces {
        margin-bottom: 0;
      }
      .impact-signal h4 {
        margin: 0 0 8px;
        font-size: 1rem;
      }
      .impact-signal > p {
        margin: 0 0 14px;
        color: #46546a;
        line-height: 1.55;
      }
    `,
  ],
})
export class RcaReportComponent {
  readonly run = input<RcaRun | null>(null);
  readonly report = input<RcaReport | null>(null);
  readonly traceState = input<TraceWaterfallLoadState>({ status: 'loading' });
  readonly retryTrace = output<void>();
  readonly status = rcaStatusLabel;
  readonly confidencePercent = (value: number): number => Math.round(value * 100);
  readonly impactSignals = parseImpactSignals;
}

function parseImpactSignals(value: string): ImpactSignals {
  const result: ImpactSignals = {
    metrics: null,
    logs: null,
    traces: null,
    fallback: null,
    hasSignals: false,
  };
  const unmatched: string[] = [];

  for (const segment of value
    .split(/[；;]/)
    .map((item) => item.trim())
    .filter(Boolean)) {
    const match = /^(Metrics|Logs|Traces?)\s*[：:]\s*(.+)$/iu.exec(segment);
    if (!match) {
      unmatched.push(segment);
      continue;
    }
    const content = match[2].trim();
    const label = match[1].toLowerCase();
    if (label === 'metrics') result.metrics = content;
    else if (label === 'logs') result.logs = content;
    else result.traces = content;
    result.hasSignals = true;
  }

  if (!result.hasSignals) result.fallback = value;
  else if (unmatched.length) result.fallback = unmatched.join('；');
  return result;
}
