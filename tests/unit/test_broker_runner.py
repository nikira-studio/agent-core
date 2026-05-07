from runner.agent_core_broker import load_broker_token


def test_broker_loads_token_from_file(tmp_path):
    token_file = tmp_path / "broker.credential"
    token_file.write_text("ac_broker_test\n", encoding="utf-8")

    assert load_broker_token("", str(token_file)) == "ac_broker_test"


def test_broker_explicit_token_wins(tmp_path):
    token_file = tmp_path / "broker.credential"
    token_file.write_text("ac_broker_file\n", encoding="utf-8")

    assert load_broker_token("ac_broker_explicit", str(token_file)) == "ac_broker_explicit"
