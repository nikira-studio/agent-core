"""Plain noise retracts itself; nothing is left for a person to review; removed surfaces say so."""

from app.database import get_db
from app.services import audit_service, memory_hygiene_service, memory_service

SCOPE = "workspace:hyg"


def _write(content, memory_class="fact", **extra):
    record, _ = memory_service.write_memory(content=content, memory_class=memory_class, scope=SCOPE, **extra)
    return record


def test_ticket_closeout_narration_retracts_itself_and_is_audited_by_rule(clean_db):
    closeout = _write("PROJ-594 closed done 2026-07-21 after the review.")
    keeper = _write("The deploy script lives in tools/deploy.sh and takes a version argument.")
    result = memory_hygiene_service.run()
    assert result["retracted"] == {"ticket_closeout": 1}
    assert memory_service.get_memory_record(closeout["id"])["record_status"] == "retracted"
    assert memory_service.get_memory_record(keeper["id"])["record_status"] == "active"
    event = audit_service.query_events(action="memory_auto_retracted", limit=3)[0]
    assert event["actor_id"] == "rule:ticket_closeout" and event["resource_id"] == closeout["id"]
    assert memory_service.restore_memory(closeout["id"])  # soft, restorable


def test_the_newest_of_a_duplicate_cluster_is_kept(clean_db):
    text = "The embedding backend is Ollama on the build host, model arctic-embed2, 1024 dimensions, no auth."
    old = _write(text)
    new = _write(text + " ")
    with get_db() as conn:  # make the order unambiguous
        conn.execute("UPDATE memory_records SET created_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (old["id"],))
        conn.commit()
    result = memory_hygiene_service.run()
    assert result["retracted"] == {"duplicate_cluster": 1}
    assert memory_service.get_memory_record(old["id"])["record_status"] == "retracted"
    assert memory_service.get_memory_record(new["id"])["record_status"] == "active"


def _age(record, iso):
    with get_db() as conn:
        conn.execute("UPDATE memory_records SET created_at = ? WHERE id = ?", (iso, record["id"]))
        conn.commit()


RELAY = 'Alex decided 2026-11-12, as relayed by the planning session: "yes to all three". Question '


def test_records_that_only_open_alike_are_not_duplicates(clean_db):
    first = _write(RELAY + '1 as put to him: "The builder merges after up-front approval". Recorded for the merge flow only.')
    second = _write(RELAY + '2 as put to him: "The builder may propose work, never starting it without your approval."')
    third = _write(RELAY + '3 as put to him: "A deploy script may run on the host, only on his click, nothing else, ever."')
    for r in (first, second, third):
        _age(r, "2026-01-01T00:00:00+00:00")
    assert memory_hygiene_service.run()["total"] == 0
    for r in (first, second, third):
        assert memory_service.get_memory_record(r["id"])["record_status"] == "active"


def test_non_decisions_that_open_alike_but_differ_are_kept_too(clean_db):
    a = _write("The nightly job reads the export folder and writes one summary per customer into the report share, then stops. A")
    b = _write("The nightly job reads the export folder and writes one summary per customer into the report share, then stops. Note: the folder moved to the new share on 11-12 and the retention changed to ninety days.")
    _age(a, "2026-01-01T00:00:00+00:00")
    _age(b, "2026-01-02T00:00:00+00:00")
    assert memory_hygiene_service.run()["total"] == 0


def test_a_decision_is_never_retracted_by_a_rule_even_as_an_exact_duplicate(clean_db):
    text = "Only a person may approve a merge into a production branch; agents may propose and prepare it."
    old = _write(text, memory_class="decision")
    new = _write(text + " ", memory_class="decision")
    _age(old, "2026-01-01T00:00:00+00:00")
    assert memory_hygiene_service.run()["total"] == 0
    assert memory_service.get_memory_record(old["id"])["record_status"] == "active"


def test_a_duplicate_written_in_the_last_day_waits(clean_db):
    text = "The embedding backend is Ollama on the build host, model arctic-embed2, 1024 dimensions, no auth."
    first = _write(text)
    second = _write(text + " ")
    _age(second, "2099-01-01T00:00:00+00:00")  # newest, kept
    # `first` was written moments ago: a burst of related writes is normal, so it waits a day
    assert memory_hygiene_service.run()["total"] == 0
    _age(first, "2026-01-01T00:00:00+00:00")
    assert memory_hygiene_service.run()["retracted"] == {"duplicate_cluster": 1}


def test_a_run_retracts_at_most_the_cap_and_leaves_the_rest_for_the_next(clean_db):
    for n in range(5):
        _write(f"STA-{600 + n} closed done 2026-07-21 after the review.")
    first = memory_hygiene_service.run(cap=3)
    assert first["total"] == 3 and first["left_for_next_run"] == 2
    assert memory_hygiene_service.run(cap=3)["total"] == 2


def test_an_ordinary_corpus_loses_nothing(clean_db):
    for content in ("Prefer SQLite for local state.", "The API listens on 3500 behind the proxy.", "Releases are tagged by the maintainer."):
        _write(content)
    assert memory_hygiene_service.run()["total"] == 0


def test_the_queue_endpoints_answer_removed_not_not_found(test_client, admin_token):
    headers = {"Authorization": f"Bearer {admin_token}"}
    for method, path in (("get", "/api/memory/proposals"), ("post", "/api/memory/proposals/generate"), ("post", "/api/memory/proposals/x/decide")):
        r = getattr(test_client, method)(path, headers=headers)
        assert r.status_code == 410 and r.json()["error"]["code"] == "REMOVED", path


def test_removed_pin_tools_say_what_happened(test_client, agent_token):
    for tool in ("memory_pin", "memory_pinned"):
        r = test_client.post("/mcp", headers={"Authorization": f"Bearer {agent_token}"}, json={"tool": tool, "params": {}})
        assert r.status_code == 410 and r.json()["error"]["code"] == "TOOL_REMOVED" and "1.3.0" in r.json()["error"]["message"]


def test_migration_closes_suggestions_that_were_pending(clean_db):
    from app.schema import _migrate_025_memory_cleanup_queue_removed

    with get_db() as conn:
        conn.execute(
            "INSERT INTO memory_proposals (id, rule, action, scope, target_ids_json, rationale, evidence_json, status, created_at) "
            "VALUES ('p1', 'stale_volatile', 'confirm', ?, '[\"x\"]', 'r', '{}', 'pending', '2026-11-01T00:00:00+00:00')",
            (SCOPE,),
        )
        _migrate_025_memory_cleanup_queue_removed(conn)
        conn.commit()
        assert conn.execute("SELECT status, decided_by FROM memory_proposals WHERE id = 'p1'").fetchone()[:] == ("rejected", "queue-removed-1.3.0")


# --- write-time refusals (agents only; plain cases only) -----------------------


def _agent_write(client, token, content, **extra):
    body = {"content": content, "memory_class": "fact", "scope": "agent:testagent", **extra}
    return client.post("/api/memory/write", headers={"Authorization": f"Bearer {token}"}, json=body)


def test_an_agent_cannot_save_a_log_of_one_occurrence(test_client, agent_token):
    r = _agent_write(test_client, agent_token, "Idle closeout heartbeat 2026-06-13T21:35Z. activity_pickup returned null.")
    assert r.status_code == 422 and r.json()["error"]["code"] == "TOO_SPECIFIC_TO_ONE_OCCURRENCE"
    assert "activity_update" in r.json()["error"]["message"]
    r = _agent_write(test_client, agent_token, "PROJ-594 closed done 2026-07-21 after the review.")
    assert r.status_code == 422 and r.json()["error"]["code"] == "TOO_SPECIFIC_TO_ONE_OCCURRENCE"
    events = audit_service.query_events(action="memory_write_refused", limit=5)
    assert len(events) == 2 and events[0]["result"] == "blocked"


def test_a_near_identical_write_names_the_record_and_says_to_read_it_first(test_client, agent_token):
    text = "The embedding backend is Ollama on the build host, model arctic-embed2, 1024 dimensions, no auth."
    first = _agent_write(test_client, agent_token, text)
    assert first.status_code == 201, first.text
    first_id = first.json()["data"]["record"]["id"]
    again = _agent_write(test_client, agent_token, text + " Updated note.")
    assert again.status_code == 422 and again.json()["error"]["code"] == "DUPLICATE_OF"
    assert first_id in again.json()["error"]["message"] and "memory_get" in again.json()["error"]["message"]
    replaced = _agent_write(test_client, agent_token, text + " Updated note.", supersedes_id=first_id)
    assert replaced.status_code == 201, replaced.text


def test_an_ordinary_write_and_a_persons_write_are_untouched(test_client, agent_token, admin_token):
    assert _agent_write(test_client, agent_token, "Prefer SQLite for local state; the file lives in data/.").status_code == 201
    person = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"content": "PROJ-594 closed done 2026-07-21 after the review.", "memory_class": "fact", "scope": "user:admin"},
    )
    assert person.status_code == 201, person.text  # a person knows what they are saving


def test_a_write_that_only_opens_like_another_is_saved_not_refused(test_client, agent_token):
    relay = 'Alex decided 2026-11-12, as relayed by the planning session: "yes to all three". Question '
    one = _agent_write(test_client, agent_token, relay + '1 as put to him: "The builder merges after up-front approval". Recorded for the merge flow.', memory_class="decision")
    two = _agent_write(test_client, agent_token, relay + '2 as put to him: "The builder may propose work, never starting it without your approval."', memory_class="decision")
    three = _agent_write(test_client, agent_token, relay + '3 as put to him: "A deploy script may run on the host, only on his click, nothing else, ever."', memory_class="decision")
    assert (one.status_code, two.status_code, three.status_code) == (201, 201, 201), (one.text, two.text, three.text)


def test_an_exact_repeat_says_so_and_does_not_offer_supersede(test_client, agent_token):
    text = "The embedding backend is Ollama on the build host, model arctic-embed2, 1024 dimensions, no auth."
    assert _agent_write(test_client, agent_token, text).status_code == 201
    again = _agent_write(test_client, agent_token, text)
    assert again.status_code == 422 and again.json()["error"]["code"] == "DUPLICATE_OF"
    assert "supersedes_id" not in again.json()["error"]["message"]


def test_a_retraction_reason_is_kept_in_the_audit_entry(test_client, agent_token):
    first = _agent_write(test_client, agent_token, "The staging host is the second rack machine and it runs the nightly build.")
    rid = first.json()["data"]["record"]["id"]
    headers = {"Authorization": f"Bearer {agent_token}"}
    done = test_client.post("/mcp", headers=headers, json={"tool": "memory_retract", "params": {"record_id": rid, "reason": "the host was decommissioned"}})
    assert done.status_code == 200, done.text
    trace = memory_service.retraction_traces([rid])[rid]
    assert trace["reason"] == "the host was decommissioned" and trace["by"]
    shown = test_client.get(f"/api/memory/{rid}", headers=headers).json()["data"]["record"]
    assert shown["retraction"]["reason"] == "the host was decommissioned"


# ---- review blocker B1: a duplicate is the same statement; a person's restore, write or confirmation is final ----

_COMMON = (
    "For the billing API the rate limit is set per tenant and enforced by the gateway in front of the service. Requests above the limit "
    "receive a 429 response with a retry hint, the counters reset every minute, and the limit applies to every route of the API alike. "
    "Changes to the limit go through the usual review and are announced to the integration teams a week before they take effect."
)
STAGING = _COMMON + " In the staging tenant it is 100 requests per minute."
PRODUCTION = _COMMON + " In the production tenant it is 900 requests per minute."


def _old(record, days=3):
    from datetime import timedelta
    from app.time_utils import utc_now
    with get_db() as conn:
        conn.execute("UPDATE memory_records SET created_at = ? WHERE id = ?", ((utc_now() - timedelta(days=days)).isoformat(), record["id"]))
        conn.commit()


def test_facts_that_differ_in_a_number_name_or_path_are_not_duplicates(clean_db):
    staging, production = _write(STAGING), _write(PRODUCTION)
    _old(staging, 4), _old(production, 3)
    assert memory_hygiene_service.run()["total"] == 0
    for r in (staging, production):
        assert memory_service.get_memory_record(r["id"])["record_status"] == "active"


def test_is_duplicate_text_means_the_same_statement():
    same = memory_service.is_duplicate_text
    assert same(STAGING, STAGING + "  ")
    assert same(STAGING, STAGING.upper())  # case alone is not a difference
    assert same(STAGING, STAGING + " Verified against the gateway config.")  # an added plain sentence
    assert not same(STAGING, PRODUCTION)  # staging/production and 100/900
    base = "The deploy runner writes its archive to /srv/builds/archive on the host and keeps seven copies for the team."
    assert not same(base, base.replace("/srv/builds/archive", "/srv/builds/archives"))  # a path
    assert not same(base, base.replace("seven", "eight"))  # a word that changed the statement
    assert not same(base, base.replace("seven copies", "7 copies"))  # a number
    assert not same(base, base + " See ticket ABC-1234 for the rollout.")  # an added identifier
    assert not same(base, base + " Owner is Maria Lopez.")  # an added name
    assert not same(base, base.replace("The deploy runner", "The Backup Runner"))  # a name


def test_a_record_a_person_restored_is_never_auto_retracted_again(clean_db):
    text = "The embedding backend is Ollama on the build host, model arctic-embed2, 1024 dimensions, no auth."
    old, new = _write(text), _write(text + " ")
    _old(old, 5), _old(new, 3)
    assert memory_hygiene_service.run()["retracted"] == {"duplicate_cluster": 1}
    assert memory_service.get_memory_record(old["id"])["record_status"] == "retracted"
    memory_service.restore_memory(old["id"], restored_by="owner")
    audit_service.write_event(actor_type="user", actor_id="owner", action="memory_restore", resource_type="memory_record", resource_id=old["id"], result="success")
    assert memory_hygiene_service.run()["total"] == 0  # the next scheduled run leaves the owner's restore alone
    assert memory_service.get_memory_record(old["id"])["record_status"] == "active"


def test_a_record_a_person_wrote_or_confirmed_is_never_auto_retracted(clean_db):
    closeout = "PROJ-594 closed done 2026-07-21 after the review."
    mine = _write(closeout, source_kind="operator_authored")
    confirmed = _write("PROJ-595 closed done 2026-07-21 after the review.")
    audit_service.write_event(actor_type="user", actor_id="owner", action="memory_confirmed", resource_type="memory_record", resource_id=confirmed["id"], result="success")
    plain = _write("PROJ-596 closed done 2026-07-21 after the review.")
    assert memory_hygiene_service.run()["retracted"] == {"ticket_closeout": 1}
    assert memory_service.get_memory_record(plain["id"])["record_status"] == "retracted"
    assert memory_service.get_memory_record(mine["id"])["record_status"] == "active"
    assert memory_service.get_memory_record(confirmed["id"])["record_status"] == "active"


def test_the_audit_entry_and_the_trace_say_which_record_it_was_a_duplicate_of(clean_db):
    text = "The embedding backend is Ollama on the build host, model arctic-embed2, 1024 dimensions, no auth."
    old, new = _write(text), _write(text + " ")
    _old(old, 5), _old(new, 3)
    memory_hygiene_service.run()
    event = audit_service.query_events(action="memory_auto_retracted", limit=1)[0]
    import json
    details = json.loads(event["details_json"]) if isinstance(event.get("details_json"), str) else event.get("details")
    assert details["duplicate_of"] == new["id"]
    assert memory_service.retraction_traces([old["id"]])[old["id"]]["duplicate_of"] == new["id"]


def test_a_write_that_differs_in_a_number_is_saved_not_refused(test_client, agent_token):
    first = _agent_write(test_client, agent_token, STAGING)
    second = _agent_write(test_client, agent_token, PRODUCTION)
    assert first.status_code == 201 and second.status_code == 201, second.text


# ---- review: a wrong refusal loses data by the agent's own hand; refuse only a bare status of one occurrence ----

def test_a_closeout_that_carries_a_lasting_statement_is_saved_not_refused(test_client, agent_token):
    lasting = ("ABC-124 closed done, and the lasting outcome: invoice exports use semicolon-delimited CSV, "
               "UTF-8 with a byte order mark, because the finance import rejects commas.")
    kept = _agent_write(test_client, agent_token, lasting)
    assert kept.status_code == 201, kept.text
    assert memory_service.get_memory_record(kept.json()["data"]["record"]["id"])["record_status"] == "active"
    bare = _agent_write(test_client, agent_token, "ABC-125 closed done 2026-07-21 after the review.")
    assert bare.status_code == 422 and bare.json()["error"]["code"] == "TOO_SPECIFIC_TO_ONE_OCCURRENCE"


def test_a_log_line_that_carries_a_lasting_statement_is_saved(test_client, agent_token):
    text = ("Idle closeout heartbeat 2026-06-13T21:35Z. The nightly import now reads the export share directly, "
            "so the copy step was removed and the share must stay mounted read-only on the build host.")
    saved = _agent_write(test_client, agent_token, text)
    assert saved.status_code == 201, saved.text
    bare = _agent_write(test_client, agent_token, "Idle closeout heartbeat 2026-06-13T21:36Z. activity_pickup returned null.")
    assert bare.status_code == 422 and bare.json()["error"]["code"] == "TOO_SPECIFIC_TO_ONE_OCCURRENCE"


def test_a_correction_without_supersedes_id_gets_a_warning_naming_the_similar_record(test_client, agent_token):
    first = _agent_write(test_client, agent_token, "Invoice exports use semicolon-delimited CSV with a byte order mark so the finance import accepts them.")
    assert first.status_code == 201
    first_id = first.json()["data"]["record"]["id"]
    second = _agent_write(test_client, agent_token, "Invoice exports use comma-delimited CSV with a byte order mark so the finance import accepts them.")
    assert second.status_code == 201, second.text  # saved, never refused
    warnings = {w["code"]: w for w in second.json()["data"].get("warnings", [])}
    assert "SIMILAR_ACTIVE_RECORD" in warnings
    assert first_id in warnings["SIMILAR_ACTIVE_RECORD"]["message"] and "supersedes_id" in warnings["SIMILAR_ACTIVE_RECORD"]["message"]
    # Replacing it on purpose raises no such warning.
    third = _agent_write(test_client, agent_token, "Invoice exports use tab-delimited CSV with a byte order mark so the finance import accepts them.", supersedes_id=first_id)
    assert third.status_code == 201 and "SIMILAR_ACTIVE_RECORD" not in {w["code"] for w in third.json()["data"].get("warnings", [])}
    unrelated = _agent_write(test_client, agent_token, "The staging database is rebuilt every Sunday night from the latest production snapshot and takes about an hour.")
    assert "SIMILAR_ACTIVE_RECORD" not in {w["code"] for w in unrelated.json()["data"].get("warnings", [])}
