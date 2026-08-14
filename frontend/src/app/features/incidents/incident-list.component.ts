import { AsyncPipe, DatePipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { RouterLink } from '@angular/router';
import { BehaviorSubject, switchMap } from 'rxjs';
import { OperatorApiClient } from '../../core/api/operator-api.client';
import {
  incidentStatusLabel,
  rcaStatusLabel,
  severityLabel,
} from '../../shared/presentation/incident-labels';

@Component({
  selector: 'app-incident-list',
  imports: [AsyncPipe, DatePipe, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<main>
    <header>
      <div>
        <p class="eyebrow">SRE AI Agent</p>
        <h1>Incident 告警中心</h1>
      </div>
      <button type="button" (click)="refresh()">重新整理</button>
    </header>
    @if (page$ | async; as page) {
      @if (page.items.length) {
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>編號</th>
                <th>雲端／專案系統</th>
                <th>告警</th>
                <th>嚴重度</th>
                <th>Incident</th>
                <th>RCA</th>
                <th>更新時間</th>
              </tr>
            </thead>
            <tbody>
              @for (item of page.items; track item.id) {
                <tr>
                  <td>
                    <a [routerLink]="['/incidents', item.id]">{{ item.incidentNumber }}</a>
                  </td>
                  <td>{{ item.provider ?? '未分類' }}／{{ item.folderCode ?? '未提供' }}</td>
                  <td>{{ item.alertName ?? item.title }}</td>
                  <td>
                    <span class="badge severity">{{ severity(item.severity) }}</span>
                  </td>
                  <td>{{ incidentStatus(item.status) }}／{{ incidentStatus(item.alertState) }}</td>
                  <td>{{ rcaStatus(item.rcaStatus) }}</td>
                  <td>{{ item.updatedAt | date: 'yyyy/MM/dd HH:mm' : 'Asia/Taipei' : 'zh-TW' }}</td>
                </tr>
              }
            </tbody>
          </table>
        </div>
      } @else {
        <p class="empty">目前沒有 Incident。</p>
      }
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
        max-width: 1180px;
        margin: auto;
        padding: 40px 24px;
      }
      header {
        display: flex;
        justify-content: space-between;
        align-items: end;
        margin-bottom: 24px;
      }
      .eyebrow {
        color: #3564d4;
        font-weight: 700;
      }
      h1 {
        margin: 0.2rem 0;
        font-size: 2rem;
      }
      button {
        background: #194fb8;
        color: white;
        border: 0;
        border-radius: 8px;
        padding: 10px 18px;
        font-weight: 700;
        cursor: pointer;
      }
      .table-wrap {
        background: white;
        border: 1px solid #dbe2ef;
        border-radius: 14px;
        overflow: auto;
        box-shadow: 0 8px 30px #1720330d;
      }
      table {
        width: 100%;
        border-collapse: collapse;
      }
      th,
      td {
        text-align: left;
        padding: 16px;
        border-bottom: 1px solid #e8edf5;
      }
      th {
        font-size: 0.8rem;
        color: #5d6b82;
        background: #f9fbfe;
      }
      a {
        color: #194fb8;
        font-weight: 700;
      }
      .badge {
        padding: 4px 8px;
        border-radius: 999px;
        background: #fff0ed;
        color: #b42318;
      }
      .empty {
        background: white;
        padding: 40px;
        border-radius: 14px;
        text-align: center;
      }
    `,
  ],
})
export class IncidentListComponent {
  private readonly api = inject(OperatorApiClient);
  private readonly reload = new BehaviorSubject<void>(undefined);
  readonly page$ = this.reload.pipe(switchMap(() => this.api.listIncidents()));
  readonly severity = severityLabel;
  readonly incidentStatus = incidentStatusLabel;
  readonly rcaStatus = rcaStatusLabel;
  refresh(): void {
    this.reload.next();
  }
}
