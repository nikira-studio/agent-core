from app.security.pii_detector import contains_pii, scan_pii


def test_email_detected():
    assert contains_pii("contact me at alice@example.com") is True
    assert contains_pii("send to bob@company.org") is True


def test_google_api_key_detected():
    assert contains_pii("AIzaSyD3cpT3nq9P_abcdefghijklmnopqrst") is True


def test_clean_text_not_flagged():
    assert contains_pii("the quick brown fox") is False
    assert contains_pii("reminder to update documentation") is False
    assert contains_pii("meeting at 3pm") is False
    assert contains_pii("phone: 1-800-555-0199") is False
    assert contains_pii("api key: EXAMPLE_LIVE_ABC123XYZ") is False
    assert contains_pii("server at 192.168.1.100") is False


def test_scan_pii_email_found():
    results = scan_pii("contact: test@test.com")
    assert "EMAIL" in results