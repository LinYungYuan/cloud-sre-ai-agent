export type Provider = 'GCP' | 'AWS';
export type CanonicalSeverity = 'SEV1' | 'SEV3' | 'UNMAPPED';
export type IncidentStatus = 'OPEN' | 'INVESTIGATING' | 'RESOLVED';
export type AlertState = 'FIRING' | 'RESOLVED';
export type RcaRunStatus =
  | 'WAITING_FOR_CLASSIFICATION'
  | 'QUEUED'
  | 'RUNNING'
  | 'SUCCEEDED'
  | 'PARTIAL'
  | 'FAILED'
  | 'CANCELLED';

export interface Scope {
  teamId: string | null;
  projectId: string | null;
  environmentId: string | null;
  serviceId: string | null;
}
export interface IncidentSummary {
  id: string;
  incidentNumber: string;
  title: string;
  severity: CanonicalSeverity;
  status: IncidentStatus;
  alertState: AlertState;
  rcaStatus: RcaRunStatus | null;
  provider: Provider | null;
  folderCode: string | null;
  alertName: string | null;
  scope: Scope;
  acknowledged: boolean;
  acknowledgedAt: string | null;
  acknowledgedBy: string | null;
  assignee: Record<string, unknown> | null;
  openedAt: string;
  updatedAt: string;
  resolvedAt: string | null;
  version: number;
}
export interface IncidentDetail extends IncidentSummary {
  description: string;
  alertIds: string[];
  rcaRunIds: string[];
}
export interface CursorPage<T> {
  items: T[];
  nextCursor: string | null;
}
export interface AlertIssue {
  rawText: string;
  source: 'grafana.annotations.AlertValues';
  contentType: 'text/plain';
  untrusted: true;
}
export interface AlertDetail {
  id: string;
  sourceId: string;
  incidentId: string | null;
  fingerprint: string;
  title: string;
  severity: CanonicalSeverity;
  state: AlertState;
  classificationStatus: 'CLASSIFIED' | 'UNCLASSIFIED';
  scope: Scope | null;
  startsAt: string;
  endsAt: string | null;
  updatedAt: string;
  provider: Provider;
  folderCode: string | null;
  alertName: string | null;
  severityRaw: string | null;
  issue: AlertIssue;
  normalization: { status: string; ruleId: string | null; ruleVersion: number | null } | null;
  normalizationWarnings: string[];
  labels: Record<string, unknown>;
  annotations: Record<string, string>;
  generatorUrl: string | null;
}
export interface RcaRun {
  id: string;
  incidentId: string;
  runNumber: number;
  status: RcaRunStatus;
  createdAt: string;
  updatedAt: string;
  startedAt: string | null;
  completedAt: string | null;
  failureCode: string | null;
  reportId: string | null;
}
export interface RcaReport {
  id: string;
  rcaRunId: string;
  incidentId: string;
  reportVersion: number;
  status: string;
  summary: string;
  rootCause: string;
  confidence: number | null;
  impact: string;
  recommendations: string[];
  hypotheses: Array<{
    statement: string;
    confidence: number;
    claims: Record<string, unknown>[];
  }>;
  claims: Record<string, unknown>[];
  createdAt: string;
}
export type TraceSpanStatus = 'OK' | 'ERROR' | 'UNSET';
export type TraceSpanKind = 'INTERNAL' | 'SERVER' | 'CLIENT' | 'PRODUCER' | 'CONSUMER';
export interface TraceWaterfallSpan {
  spanId: string;
  parentSpanId: string | null;
  serviceName: string;
  operationName: string;
  startOffsetMs: number;
  durationMs: number;
  status: TraceSpanStatus;
  kind: TraceSpanKind;
  criticalPath: boolean;
  attributes: Record<string, string | number | boolean>;
}
export interface TraceWaterfall {
  schemaVersion: 1;
  traceId: string;
  rootServiceName: string;
  rootOperationName: string;
  startedAt: string;
  durationMs: number;
  spanCount: number;
  representativeScore: number;
  truncated: boolean;
  spans: TraceWaterfallSpan[];
}
export interface TraceWaterfallResponse {
  trace: TraceWaterfall | null;
}
export type TraceWaterfallLoadState =
  | { status: 'loading' }
  | { status: 'empty' }
  | { status: 'error' }
  | { status: 'ready'; trace: TraceWaterfall };
export interface ProblemDetails {
  type: string;
  title: string;
  status: number;
  code: string;
  detail: string;
  correlationId?: string;
}
