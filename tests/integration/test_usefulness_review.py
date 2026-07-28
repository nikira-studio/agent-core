import json

from app.database import get_db
from app.services import memory_proposal_service, memory_service, usefulness_service

# Deliberately generic sample records: this suite ships in a public repository,
# so nothing here describes anyone's real infrastructure.
VAGUE = (
    "On 2026-05-16 the production email deploy failed and was traced to the "
    "standalone runner missing dependencies for the startup migration script."
)
ACTIONABLE = (
    "Root cause of the failed deploy: the runner image lacked libpq-dev, so the "
    "migration script could not build psycopg2. Add it to the image."
)


def _write(content=VAGUE, memory_class="fact", scope="workspace:proj"):
    record, _ = memory_service.write_memory(
        content=content, memory_class=memory_class, scope=scope
    )
    return record


def _configure(binding_id="binding-1", enabled=True, limit=20, provider="binding"):
    rows = {
        # How to reach a model: shared by every feature that needs judgement.
        "review_model_provider": provider,
        "review_model_binding_id": binding_id,
        "review_model_name": "test-model",
        # Whether this particular feature is on.
        "usefulness_review_enabled": "1" if enabled else "0",
        "usefulness_review_limit": str(limit),
    }
    with get_db() as conn:
        for key, value in rows.items():
            conn.execute(
                "INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        conn.commit()


def _fake_model(monkeypatch, reply, *, success=True, capture=None):
    from app.services import connector_service

    def fake(binding_id, action, params=None):
        if capture is not None:
            capture.append({"binding_id": binding_id, "action": action, "params": params})
        if not success:
            return {"success": False, "error": "model unavailable"}
        body = reply if isinstance(reply, str) else json.dumps(reply)
        return {
            "success": True,
            "data": {"choices": [{"message": {"role": "assistant", "content": body}}]},
        }

    monkeypatch.setattr(connector_service, "execute_binding_action_with_logging", fake)


# --- off by default --------------------------------------------------------


def test_nothing_runs_until_an_operator_turns_it_on(clean_db):
    """Judging a record sends its content to a model. That is opt-in."""
    _write()
    result = usefulness_service.review_scope("workspace:proj")
    assert result["ready"] is False
    assert result["reviewed"] == 0
    assert "No review model configured" in result["reason"]


def test_a_model_alone_is_not_enough(clean_db):
    """Configuring a model enables the capability; the feature is still opt-in."""
    _configure(enabled=False)
    assert usefulness_service.review_scope("workspace:proj")["ready"] is False


def test_the_feature_alone_is_not_enough(clean_db):
    """And turning the feature on without a model degrades, it does not fail."""
    from app.database import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO system_settings (key, value) VALUES "
            "('usefulness_review_enabled', '1')"
        )
        conn.commit()
    result = usefulness_service.review_scope("workspace:proj")
    assert result["ready"] is False
    assert "No review model configured" in result["reason"]


def test_no_model_call_is_made_while_disabled(clean_db, monkeypatch):
    calls = []
    _fake_model(monkeypatch, {"verdict": "low_value", "reason": "x"}, capture=calls)
    _write()
    usefulness_service.review_scope("workspace:proj")
    assert calls == [], "record content must not leave the machine when disabled"


def test_the_endpoint_says_what_is_missing(test_client, admin_token):
    r = test_client.post(
        "/api/memory/proposals/review-usefulness",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "REVIEWER_NOT_CONFIGURED"


# --- the pre-filter --------------------------------------------------------


def test_records_that_name_something_concrete_are_not_sent(clean_db, monkeypatch):
    """Model calls are spent on the ambiguous ones."""
    calls = []
    _fake_model(monkeypatch, {"verdict": "keep", "reason": "ok"}, capture=calls)
    _configure()
    _write(content=ACTIONABLE)
    _write(content="Do not commit generated files to the repository.")
    _write(content='The ingest route is `POST /api/webhook/health`.')

    result = usefulness_service.review_scope("workspace:proj")
    assert result["reviewed"] == 0
    assert calls == []


def test_a_narrated_record_with_no_specifics_is_sent(clean_db, monkeypatch):
    calls = []
    _fake_model(monkeypatch, {"verdict": "low_value", "reason": "names no cause"}, capture=calls)
    _configure()
    _write(content=VAGUE)

    result = usefulness_service.review_scope("workspace:proj")
    assert result["reviewed"] == 1
    assert len(calls) == 1
    assert calls[0]["params"]["model"] == "test-model"
    assert VAGUE[:40] in calls[0]["params"]["messages"][0]["content"]


# --- verdicts --------------------------------------------------------------


def test_a_low_value_verdict_becomes_a_proposal_carrying_the_reason(clean_db, monkeypatch):
    _fake_model(monkeypatch, {"verdict": "low_value", "reason": "reports a failure without naming the cause"})
    _configure()
    record = _write()

    result = usefulness_service.review_scope("workspace:proj")
    assert result["low_value"] == 1
    assert result["proposals_queued"] == 1

    proposal = memory_proposal_service.list_proposals(rule="low_value")[0]
    assert proposal["target_ids"] == [record["id"]]
    assert "without naming the cause" in proposal["rationale"]
    # The rationale must not present an opinion as a measurement.
    assert "opinion, not a measurement" in proposal["rationale"]
    assert proposal["prompt"] == "Is there anything here a future session could act on?"


def test_a_keep_verdict_changes_nothing(clean_db, monkeypatch):
    _fake_model(monkeypatch, {"verdict": "keep", "reason": "names the fix"})
    _configure()
    record = _write()

    result = usefulness_service.review_scope("workspace:proj")
    assert result["kept"] == 1
    assert result["proposals_queued"] == 0
    assert memory_service.get_memory_record(record["id"])["record_status"] == "active"


def test_the_record_is_never_touched_directly(clean_db, monkeypatch):
    """The judge proposes. It has no path to changing a record itself."""
    _fake_model(monkeypatch, {"verdict": "low_value", "reason": "vague"})
    _configure()
    record = _write()
    usefulness_service.review_scope("workspace:proj")

    stored = memory_service.get_memory_record(record["id"])
    assert stored["record_status"] == "active"
    assert stored["last_confirmed_at"] is None


def test_this_rule_can_never_be_automated(clean_db):
    """Other rules could earn automation from a good record. Not this one."""
    assert "low_value" in usefulness_service.NEVER_AUTOMATED


# --- refusing to guess -----------------------------------------------------


def test_unparseable_output_is_skipped_not_guessed(clean_db, monkeypatch):
    _fake_model(monkeypatch, "I think this one is probably fine, honestly.")
    _configure()
    _write()

    result = usefulness_service.review_scope("workspace:proj")
    assert result["unjudged"] == 1
    assert result["low_value"] == 0
    assert result["proposals_queued"] == 0


def test_an_unexpected_verdict_word_is_skipped(clean_db, monkeypatch):
    _fake_model(monkeypatch, {"verdict": "maybe", "reason": "unsure"})
    _configure()
    _write()
    assert usefulness_service.review_scope("workspace:proj")["unjudged"] == 1


def test_a_failing_model_does_not_produce_verdicts(clean_db, monkeypatch):
    _fake_model(monkeypatch, {"verdict": "low_value", "reason": "x"}, success=False)
    _configure()
    _write()

    result = usefulness_service.review_scope("workspace:proj")
    assert result["unjudged"] == 1
    assert result["proposals_queued"] == 0


def test_prose_around_the_json_is_tolerated(clean_db, monkeypatch):
    """Small models wrap JSON in commentary; that should not cost a verdict."""
    _fake_model(
        monkeypatch,
        'Sure! Here is my assessment:\n{"verdict": "low_value", "reason": "no specifics"}\nHope that helps.',
    )
    _configure()
    _write()
    assert usefulness_service.review_scope("workspace:proj")["low_value"] == 1


def test_an_ollama_shaped_reply_is_understood(clean_db, monkeypatch):
    """The binding could be any provider, so the response shape varies."""
    from app.services import connector_service

    def fake(binding_id, action, params=None):
        return {
            "success": True,
            "data": {"message": {"content": '{"verdict": "low_value", "reason": "vague"}'}},
        }

    monkeypatch.setattr(connector_service, "execute_binding_action_with_logging", fake)
    _configure()
    _write()
    assert usefulness_service.review_scope("workspace:proj")["low_value"] == 1


# --- cost control ----------------------------------------------------------


def test_the_batch_is_capped(clean_db, monkeypatch):
    calls = []
    _fake_model(monkeypatch, {"verdict": "keep", "reason": "fine"}, capture=calls)
    _configure(limit=2)
    for i in range(5):
        _write(content=f"{VAGUE} Attempt {i}.")

    result = usefulness_service.review_scope("workspace:proj")
    assert result["reviewed"] == 2
    assert len(calls) == 2


def test_a_decided_record_is_not_re_reviewed(clean_db, monkeypatch):
    _fake_model(monkeypatch, {"verdict": "low_value", "reason": "vague"})
    _configure()
    _write()

    first = usefulness_service.review_scope("workspace:proj")
    second = usefulness_service.review_scope("workspace:proj")
    assert first["proposals_queued"] == 1
    assert second["proposals_queued"] == 0, "the queue must converge, not re-ask"
