from pi_gw_panel.auth.auth import new_csrf_token, csrf_matches


def test_csrf_token_roundtrip():
    tok = new_csrf_token()
    assert isinstance(tok, str) and len(tok) == 43
    assert csrf_matches(tok, tok) is True
    assert csrf_matches(tok, "other") is False
    assert csrf_matches(None, tok) is False
    assert csrf_matches(tok, None) is False
