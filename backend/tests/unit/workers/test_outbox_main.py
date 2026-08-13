from sre_agent.workers.outbox_main import main


def test_outbox_main_returns_zero_for_success() -> None:
    assert main(run=lambda: None) == 0


def test_outbox_main_returns_one_without_printing_sensitive_error(capsys) -> None:
    def fail() -> None:
        raise RuntimeError("token=must-not-leak")

    assert main(run=fail) == 1
    captured = capsys.readouterr()
    assert "must-not-leak" not in captured.out
    assert "must-not-leak" not in captured.err
