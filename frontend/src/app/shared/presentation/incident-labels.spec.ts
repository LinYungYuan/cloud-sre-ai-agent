import { rcaStatusLabel, severityLabel, warningLabel } from './incident-labels';

describe('Traditional Chinese presentation labels', () => {
  it('renders unmapped severity and normalization warnings', () => {
    expect(severityLabel('UNMAPPED')).toBe('嚴重度未映射');
    expect(warningLabel('gcp_project_id_blank')).toBe('GCP project ID 空白');
    expect(warningLabel('resource_unclassified')).toBe('資源未分類');
    expect(warningLabel('rule_conflict')).toBe('規則衝突');
    expect(rcaStatusLabel('RUNNING')).toBe('分析中');
  });
});
