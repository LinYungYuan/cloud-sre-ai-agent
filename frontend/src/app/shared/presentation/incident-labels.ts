import type { CanonicalSeverity, RcaRunStatus } from '../../core/api/operator-api.models';

export const severityLabel = (value: CanonicalSeverity): string =>
  ({ SEV1: '最高嚴重度', SEV3: '警告', UNMAPPED: '嚴重度未映射' })[value];
export const incidentStatusLabel = (value: string): string =>
  ({ OPEN: '處理中', INVESTIGATING: '調查中', RESOLVED: '已結案', FIRING: '告警中' })[value] ??
  value;
export const rcaStatusLabel = (value: RcaRunStatus | null): string =>
  value === null
    ? '尚未建立'
    : {
        WAITING_FOR_CLASSIFICATION: '等待分類',
        QUEUED: '等待分析',
        RUNNING: '分析中',
        SUCCEEDED: '已完成',
        PARTIAL: '部分完成',
        FAILED: '失敗',
        CANCELLED: '已取消',
      }[value];
export const warningLabel = (value: string): string =>
  ({
    gcp_project_id_blank: 'GCP project ID 空白',
    resource_unclassified: '資源未分類',
    rule_conflict: '規則衝突',
    severity_unmapped: '嚴重度未映射',
  })[value] ?? value;
