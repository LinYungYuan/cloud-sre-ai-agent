import { AsyncPipe, DatePipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { BehaviorSubject, forkJoin, Observable, of, switchMap } from 'rxjs';
import { OperatorApiClient } from '../../core/api/operator-api.client';
import type {
  AlertDetail,
  IncidentDetail,
  RcaReport,
  RcaRun,
} from '../../core/api/operator-api.models';
import { AlertDetailComponent } from '../alerts/alert-detail.component';
import { RcaReportComponent } from '../rca/rca-report.component';
import { incidentStatusLabel, severityLabel } from '../../shared/presentation/incident-labels';

@Component({
  selector: 'app-incident-detail',
  imports: [AsyncPipe, DatePipe, RouterLink, AlertDetailComponent, RcaReportComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<main>
    <nav>
      <a routerLink="/incidents">← 返回 Incident 列表</a
      ><button type="button" (click)="refresh()">重新整理</button>
    </nav>
    @if (view$ | async; as view) {
      <article>
        <p class="eyebrow">{{ view.incident.incidentNumber }}</p>
        <h1>{{ view.incident.title }}</h1>
        <div class="summary">
          <span>{{ severity(view.incident.severity) }}</span
          ><span>{{ status(view.incident.status) }}</span
          ><span
            >更新
            {{ view.incident.updatedAt | date: 'yyyy/MM/dd HH:mm' : 'Asia/Taipei' : 'zh-TW' }}</span
          >
        </div>
        <p>{{ view.incident.description }}</p>
      </article>
      <app-alert-detail [alert]="view.alert" /><app-rca-report
        [run]="view.run"
        [report]="view.report"
      />
    } @else {
      <p>載入中…</p>
    }
  </main>`,
  styles: [
    `
      :host {
        display: block;
        min-height: 100vh;
        background: #f4f7fb;
        color: #172033;
        font-family: system-ui, sans-serif;
      }
      main {
        max-width: 980px;
        margin: auto;
        padding: 32px 20px;
      }
      nav {
        display: flex;
        justify-content: space-between;
        margin-bottom: 20px;
      }
      button {
        background: #194fb8;
        color: #fff;
        border: 0;
        border-radius: 8px;
        padding: 9px 16px;
      }
      article,
      app-alert-detail,
      app-rca-report {
        display: block;
        background: white;
        border: 1px solid #dbe2ef;
        border-radius: 14px;
        padding: 24px;
        margin-bottom: 18px;
      }
      .eyebrow,
      a {
        color: #194fb8;
        font-weight: 700;
      }
      .summary {
        display: flex;
        gap: 10px;
        flex-wrap: wrap;
      }
      .summary span {
        background: #edf3ff;
        border-radius: 999px;
        padding: 6px 10px;
      }
    `,
  ],
})
export class IncidentDetailComponent {
  private readonly api = inject(OperatorApiClient);
  private readonly id = inject(ActivatedRoute).snapshot.paramMap.get('id')!;
  private readonly reload = new BehaviorSubject<void>(undefined);
  readonly view$: Observable<{
    incident: IncidentDetail;
    alert: AlertDetail | null;
    run: RcaRun | null;
    report: RcaReport | null;
  }> = this.reload.pipe(
    switchMap(() => this.api.getIncident(this.id)),
    switchMap((incident) =>
      forkJoin({
        incident: of(incident),
        alert: incident.alertIds[0] ? this.api.getAlert(incident.alertIds[0]) : of(null),
        runs: this.api.listRcaRuns(incident.id),
      }),
    ),
    switchMap(({ incident, alert, runs }) => {
      const run = runs.items[0] ?? null;
      const report: Observable<RcaReport | null> = run?.reportId
        ? this.api.getRcaReport(run.id)
        : of(null);
      return forkJoin({ incident: of(incident), alert: of(alert), run: of(run), report });
    }),
  );
  readonly severity = severityLabel;
  readonly status = incidentStatusLabel;
  refresh(): void {
    this.reload.next();
  }
}
