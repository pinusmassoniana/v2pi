from pi_gw_panel.logs import tail


def test_tail_returns_last_n_lines(tmp_path):
    p = tmp_path / "x.log"
    p.write_text("\n".join(f"line{i}" for i in range(10)) + "\n")
    assert tail(str(p), 3) == ["line7", "line8", "line9"]
    assert tail(str(p), 100) == [f"line{i}" for i in range(10)]


def test_tail_missing_file_is_empty(tmp_path):
    assert tail(str(tmp_path / "nope.log"), 5) == []
