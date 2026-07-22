import logging

from pi_gw_panel.logs import setup_app_logging, teardown_app_logging, tail


def test_tail_returns_last_n_lines(tmp_path):
    p = tmp_path / "x.log"
    p.write_text("\n".join(f"line{i}" for i in range(10)) + "\n")
    assert tail(str(p), 3) == ["line7", "line8", "line9"]
    assert tail(str(p), 100) == [f"line{i}" for i in range(10)]


def test_tail_missing_file_is_empty(tmp_path):
    assert tail(str(tmp_path / "nope.log"), 5) == []


def test_app_logging_handler_has_explicit_lifecycle(tmp_path):
    root = logging.getLogger()
    before = list(root.handlers)
    handler = setup_app_logging(str(tmp_path / "app.log"))
    assert handler in root.handlers
    teardown_app_logging(handler)
    assert root.handlers == before
