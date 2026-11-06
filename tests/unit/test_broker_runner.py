from runner.agent_core_broker import load_broker_token


def test_broker_loads_token_from_file(tmp_path):
    token_file = tmp_path / "broker.credential"
    token_file.write_text("ac_broker_test\n", encoding="utf-8")

    assert load_broker_token("", str(token_file)) == "ac_broker_test"


def test_broker_explicit_token_wins(tmp_path):
    token_file = tmp_path / "broker.credential"
    token_file.write_text("ac_broker_file\n", encoding="utf-8")

    assert load_broker_token("ac_broker_explicit", str(token_file)) == "ac_broker_explicit"


def test_plain_http_is_fine_to_this_machine_and_https_anywhere():
    from runner.agent_core_broker import check_transport

    for url in ("http://localhost:3500", "http://127.0.0.1:3500", "http://[::1]:3500", "https://core.example.com"):
        assert check_transport(url, "refuse") == (True, "")


def test_plain_http_to_another_host_warns_or_refuses_by_policy_and_can_be_allowed():
    from runner.agent_core_broker import check_transport

    for url in ("http://core.example.com", "http://192.168.1.20:3500", "http://agent-core:3500"):
        ok, message = check_transport(url, "warn")
        assert ok and message.startswith("WARNING") and "unencrypted" in message
        ok, message = check_transport(url, "refuse")
        assert not ok and message.startswith("ERROR")
        assert check_transport(url, "refuse", allow_plain_http=True) == (True, "")


def test_other_schemes_are_refused():
    from runner.agent_core_broker import check_transport

    for url in ("file:///etc/passwd", "ftp://host/", "core.example.com"):
        ok, message = check_transport(url, "warn")
        assert not ok and "http:// or https://" in message


def test_the_documented_double_dash_form_runs_the_command(tmp_path, monkeypatch, capsys):
    import sys

    from runner import agent_core_broker as broker

    seen = {}
    monkeypatch.setattr(broker, "run_env_mode", lambda agent, cmd, token, url: seen.update(cmd=cmd) or 0)
    for argv in (["x", "--agent-id", "a", "--token", "t", "--", "echo", "hi"], ["x", "--agent-id", "a", "--token", "t", "echo", "hi"]):
        monkeypatch.setattr(sys, "argv", argv)
        seen.clear()
        assert broker.main() == 0 and seen["cmd"] == ["echo", "hi"]
