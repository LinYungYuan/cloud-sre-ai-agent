import { HttpClient, HttpErrorResponse, HttpParams } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { catchError, Observable, throwError } from 'rxjs';

import { RUNTIME_CONFIG } from '../runtime-config/runtime-config';
import type {
  AlertDetail,
  CursorPage,
  IncidentDetail,
  IncidentSummary,
  ProblemDetails,
  RcaReport,
  RcaRun,
  TraceWaterfallResponse,
} from './operator-api.models';

export class OperatorApiError extends Error {
  constructor(readonly problem: ProblemDetails) {
    super(problem.detail);
  }
}

@Injectable({ providedIn: 'root' })
export class OperatorApiClient {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = inject(RUNTIME_CONFIG).apiBaseUrl.replace(/\/$/, '');

  listIncidents(cursor?: string, limit = 50): Observable<CursorPage<IncidentSummary>> {
    let params = new HttpParams().set('limit', limit);
    if (cursor) params = params.set('cursor', cursor);
    return this.get<CursorPage<IncidentSummary>>('/incidents', params);
  }
  getIncident(id: string): Observable<IncidentDetail> {
    return this.get(`/incidents/${encodeURIComponent(id)}`);
  }
  getAlert(id: string): Observable<AlertDetail> {
    return this.get(`/alerts/${encodeURIComponent(id)}`);
  }
  listRcaRuns(incidentId: string, cursor?: string, limit = 50): Observable<CursorPage<RcaRun>> {
    let params = new HttpParams().set('limit', limit);
    if (cursor) params = params.set('cursor', cursor);
    return this.get(`/incidents/${encodeURIComponent(incidentId)}/rca-runs`, params);
  }
  getRcaReport(runId: string): Observable<RcaReport> {
    return this.get(`/rca-runs/${encodeURIComponent(runId)}/report`);
  }
  getTraceWaterfall(runId: string): Observable<TraceWaterfallResponse> {
    return this.get(`/rca-runs/${encodeURIComponent(runId)}/trace-waterfall`);
  }

  private get<T>(path: string, params?: HttpParams): Observable<T> {
    return this.http.get<T>(`${this.baseUrl}${path}`, { params }).pipe(
      catchError((error: HttpErrorResponse) => {
        const body = error.error as Partial<ProblemDetails> | null;
        return throwError(
          () =>
            new OperatorApiError({
              type: body?.type ?? 'about:blank',
              title: body?.title ?? '讀取失敗',
              status: body?.status ?? error.status,
              code: body?.code ?? 'HTTP_ERROR',
              detail: body?.detail ?? '目前無法取得資料，請稍後再試。',
              correlationId: body?.correlationId,
            }),
        );
      }),
    );
  }
}
