import { ChangeDetectionStrategy, Component, computed, input, output, signal } from '@angular/core';
import type {
  TraceWaterfall,
  TraceWaterfallLoadState,
  TraceWaterfallSpan,
} from '../../core/api/operator-api.models';

interface TraceWaterfallRow {
  span: TraceWaterfallSpan;
  depth: number;
}

interface TraceSelection {
  traceId: string;
  spanId: string;
}

const SERVICE_COLORS = ['#0f766e', '#2563eb', '#7c3aed', '#b45309', '#be185d', '#0369a1'];
const CRITICAL_COLOR = '#c2413b';

@Component({
  selector: 'app-trace-waterfall',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<section class="trace-waterfall" aria-labelledby="trace-waterfall-heading">
    <h3 id="trace-waterfall-heading">Trace 瀑布圖</h3>
    @if (readyTrace(); as trace) {
      <div class="trace-summary">
        <span class="duration">{{ formatDuration(trace.durationMs) }}</span>
        <span class="trace-count">{{ trace.spanCount }} spans</span>
        <span class="critical-badge">Critical path</span>
      </div>
      @if (trace.truncated) {
        <p class="truncated" role="status">僅顯示前 100 個關鍵 Span；其餘 Span 已省略。</p>
      }
      <div class="timeline-scroll" tabindex="0" aria-label="Trace span timeline，可水平捲動">
        <div class="timeline">
          <div class="timeline-axis" aria-hidden="true">
            <span>0ms</span><span>{{ formatDuration(trace.durationMs) }}</span>
          </div>
          <div class="span-tree" role="tree" aria-label="Trace spans">
            @for (row of rows(); track row.span.spanId) {
              <div class="span-row">
                <button
                  type="button"
                  role="treeitem"
                  class="span-label"
                  [attr.data-span-id]="row.span.spanId"
                  [attr.aria-level]="row.depth + 1"
                  [attr.aria-selected]="isSelected(row.span.spanId)"
                  [class.selected]="isSelected(row.span.spanId)"
                  [style.padding-left.rem]="0.75 + row.depth * 1.25"
                  (click)="selectSpan(row.span.spanId)"
                  (keydown)="onRowKeydown($event, row.span.spanId)"
                >
                  <span class="service">{{ row.span.serviceName }}</span>
                  <span class="operation">{{ row.span.operationName }}</span>
                </button>
                <div class="bar-track" aria-hidden="true">
                  <span
                    class="bar"
                    [class.error]="row.span.status === 'ERROR' || row.span.criticalPath"
                    [style.background]="barColor(row.span)"
                    [style.left]="barStyle(row.span, trace.durationMs).left"
                    [style.width]="barStyle(row.span, trace.durationMs).width"
                  ></span>
                </div>
              </div>
            }
          </div>
        </div>
      </div>
      @if (selectedSpan(); as span) {
        <section class="details" data-trace-details aria-live="polite" aria-label="選取的 Span 詳細資料">
          <h4>{{ span.operationName }}</h4>
          <dl>
            <div><dt>服務</dt><dd>{{ span.serviceName }}</dd></div>
            <div><dt>耗時</dt><dd>{{ formatDuration(span.durationMs) }}</dd></div>
            <div><dt>狀態</dt><dd>{{ span.status }}</dd></div>
            <div><dt>類型</dt><dd>{{ span.kind }}</dd></div>
          </dl>
        </section>
      }
    } @else if (state().status === 'loading') {
      <div class="loading" data-trace-loading aria-label="Trace 載入中">
        <span></span><span></span><span></span>
      </div>
    } @else if (state().status === 'error') {
      <div class="message error" role="alert">
        <p>Trace 資料載入失敗，請稍後再試。</p>
        <button type="button" data-trace-retry (click)="retry.emit()">重試</button>
      </div>
    } @else {
      <p class="message">目前沒有可顯示的 Trace 資料。</p>
    }
  </section>`,
  styles: [
    `
      :host { display: block; }
      .trace-waterfall { color: #172033; }
      h3 { margin: 0 0 12px; font-size: 1.1rem; }
      .trace-summary { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; color: #5d6b82; font-size: .875rem; margin-bottom: 12px; }
      .duration { color: #172033; font-size: 1.1rem; font-weight: 750; }
      .critical-badge { color: #9f2520; background: #fff0ed; border-radius: 999px; padding: 3px 8px; font-weight: 700; }
      .truncated { margin: 0 0 12px; padding: 9px 12px; border-left: 3px solid #c2413b; background: #fff7f5; color: #7c2d12; font-size: .875rem; }
      .timeline-scroll { overflow-x: auto; border: 1px solid #dbe2ef; border-radius: 10px; background: #fbfcff; }
      .timeline-scroll:focus-visible { outline: 3px solid #194fb8; outline-offset: 2px; }
      .timeline { min-width: 680px; }
      .timeline-axis { display: flex; justify-content: space-between; margin-left: 42%; padding: 8px 12px; color: #5d6b82; font-size: .75rem; border-bottom: 1px solid #e8edf5; }
      .span-row { display: grid; grid-template-columns: 42% 58%; min-height: 44px; border-bottom: 1px solid #eef2f7; }
      .span-row:last-child { border-bottom: 0; }
      .span-label { min-width: 0; display: flex; gap: 6px; align-items: baseline; border: 0; border-right: 1px solid #e8edf5; background: transparent; color: #172033; text-align: left; cursor: pointer; overflow: hidden; }
      .span-label.selected { background: #edf3ff; }
      .span-label:focus-visible { outline: 3px solid #194fb8; outline-offset: -3px; position: relative; z-index: 1; }
      .service { flex: 0 0 auto; font-weight: 700; white-space: nowrap; }
      .operation { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #5d6b82; }
      .bar-track { position: relative; min-width: 0; background: repeating-linear-gradient(to right, transparent 0, transparent calc(25% - 1px), #e8edf5 calc(25% - 1px), #e8edf5 25%); }
      .bar { position: absolute; top: 13px; height: 17px; min-width: 2px; border-radius: 4px; opacity: .9; }
      .bar.error { box-shadow: inset 0 0 0 1px #8f211d; }
      .details { margin-top: 12px; padding: 14px; border-radius: 10px; background: #f4f7fb; }
      h4 { margin: 0 0 8px; font-size: 1rem; }
      dl { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); margin: 0; gap: 8px; }
      dl div { min-width: 0; } dt { color: #5d6b82; font-size: .75rem; } dd { margin: 2px 0 0; overflow-wrap: anywhere; font-weight: 650; }
      .loading { display: grid; gap: 8px; } .loading span { height: 24px; border-radius: 6px; background: linear-gradient(90deg, #edf1f7 25%, #f7f9fc 50%, #edf1f7 75%); background-size: 200% 100%; animation: pulse 1.2s linear infinite; }
      .loading span:nth-child(2) { width: 82%; } .loading span:nth-child(3) { width: 68%; }
      .message { margin: 0; padding: 18px; border-radius: 10px; background: #f4f7fb; color: #46546a; } .message.error { background: #fff4f2; color: #8f211d; } .message p { margin: 0 0 10px; }
      .message button { border: 0; border-radius: 7px; padding: 8px 12px; color: white; background: #194fb8; font-weight: 700; cursor: pointer; } .message button:focus-visible { outline: 3px solid #172033; outline-offset: 2px; }
      @keyframes pulse { to { background-position: -200% 0; } }
    `,
  ],
})
export class TraceWaterfallComponent {
  readonly state = input<TraceWaterfallLoadState>({ status: 'loading' });
  readonly retry = output<void>();

  private readonly selection = signal<TraceSelection | null>(null);
  readonly readyTrace = computed(() => {
    const state = this.state();
    return state.status === 'ready' ? state.trace : null;
  });
  readonly rows = computed(() => buildRows(this.readyTrace()));
  readonly selectedSpan = computed(() => {
    const trace = this.readyTrace();
    if (!trace) return null;
    const selection = this.selection();
    return selection?.traceId === trace.traceId
      ? trace.spans.find((span) => span.spanId === selection.spanId) ?? defaultSpan(trace)
      : defaultSpan(trace);
  });

  selectSpan(spanId: string): void {
    const trace = this.readyTrace();
    if (trace) this.selection.set({ traceId: trace.traceId, spanId });
  }

  onRowKeydown(event: KeyboardEvent, spanId: string): void {
    if (event.key === 'Enter') {
      event.preventDefault();
      this.selectSpan(spanId);
    }
  }

  isSelected(spanId: string): boolean {
    return this.selectedSpan()?.spanId === spanId;
  }

  formatDuration(value: number): string {
    return `${new Intl.NumberFormat('en-US').format(Math.round(value))}ms`;
  }

  barStyle(span: TraceWaterfallSpan, total: number): { left: string; width: string } {
    const safeTotal = Number.isFinite(total) && total > 0 ? total : 1;
    const offset = Number.isFinite(span.startOffsetMs) ? span.startOffsetMs : 0;
    const duration = Number.isFinite(span.durationMs) ? span.durationMs : 0;
    return {
      left: `${clampPercent((offset / safeTotal) * 100)}%`,
      width: `${Math.max(0.5, clampPercent((duration / safeTotal) * 100))}%`,
    };
  }

  barColor(span: TraceWaterfallSpan): string {
    if (span.status === 'ERROR' || span.criticalPath) return CRITICAL_COLOR;
    return SERVICE_COLORS[stableHash(span.serviceName) % SERVICE_COLORS.length];
  }
}

function buildRows(trace: TraceWaterfall | null): TraceWaterfallRow[] {
  if (!trace) return [];
  const spansById = new Map(trace.spans.map((span) => [span.spanId, span]));
  const depthFor = (span: TraceWaterfallSpan, visiting = new Set<string>()): number => {
    if (!span.parentSpanId || visiting.has(span.spanId)) return 0;
    const parent = spansById.get(span.parentSpanId);
    if (!parent) return 0;
    const next = new Set(visiting);
    next.add(span.spanId);
    return depthFor(parent, next) + 1;
  };
  return trace.spans.map((span) => ({ span, depth: depthFor(span) }));
}

function defaultSpan(trace: TraceWaterfall): TraceWaterfallSpan | null {
  for (let index = trace.spans.length - 1; index >= 0; index -= 1) {
    const span = trace.spans[index];
    if (span.status === 'ERROR' && span.criticalPath) return span;
  }
  return trace.spans.find((span) => span.parentSpanId === null) ?? trace.spans[0] ?? null;
}

function clampPercent(value: number): number {
  return Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0;
}

function stableHash(value: string): number {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash * 31 + value.charCodeAt(index)) >>> 0;
  }
  return hash;
}
