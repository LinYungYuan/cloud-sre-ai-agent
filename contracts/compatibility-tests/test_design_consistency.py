from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_platform_design_no_longer_contains_superseded_rca_or_chat_rules() -> None:
    text = (
        ROOT / "docs/superpowers/specs/2026-08-12-observability-rca-platform-design.md"
    ).read_text()

    for superseded in (
        "解析 team/project/environment/service scope",
        "team/environment/service 尚未完成分類",
        "本階段不實作 Router",
        "共享 AI 對話",
        "raw_result_reference",
    ):
        assert superseded not in text
    assert "resource.label.project_id" in text
    assert "folder + alertname" in text
    assert "Rule Router" in text
    assert "Chat" in text and "不屬於目前 release scope" in text
