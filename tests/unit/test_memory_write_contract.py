import pytest

from app.time_utils import parse_utc_datetime


@pytest.fixture(autouse=True)
def _db(clean_db):
    pass


# Per-occurrence logs unambiguous enough to both warn and auto-expire.
EXPIRING_SAMPLES = [
    "SAG-638 routine fallback sweep heartbeat (2026-06-07 15:16 UTC). Continuation tick.",
    "NIKA-731 silent-run review for CTO completed 2026-06-18T11:34:35Z. Finding: FALSE POSITIVE.",
    "STA-112 fire #59 close 2026-06-24T11:32Z. Sixteenth wake on STA-112.",
    "Idle closeout heartbeat 2026-06-13T21:35Z. activity_pickup returned null.",
]

# Warned about, but not expired: a closeout often carries a durable payload.
EPISODIC_SAMPLES = EXPIRING_SAMPLES + [
    "STA-220 closed done 2026-06-24T21:32:29Z. Fire of the IT Services Wake Recovery Monitor.",
]

DURABLE_SAMPLES = [
    "Do NOT edit vendored dependencies directly; the upstream framework is pulled frequently so core edits get clobbered.",
    "Cloudflare at cdn.example.com blocks Python-urllib User-Agents with 1010. Allows Mozilla/5.0.",
    "Telegram supergroup chat_ids need the -100 prefix in deliver and send_message paths.",
]


# Records drawn from the live corpus that a naive "contains the word heartbeat"
# rule flagged, but that are durable knowledge and must never auto-expire.
FALSE_POSITIVE_SAMPLES = [
    "Paperclip startup and periodic heartbeat recovery run scanSilentActiveRuns() and "
    "reconcileProductivityReviews() on a fixed scheduler; HEARTBEAT_SCHEDULER_INTERVAL_MS "
    "defaults to 30000 ms.",
    "Paperclip config is read once at startup — there is no live config reload, no SIGHUP "
    "handler, and no PATCH /api/config route, as of 2026-06-11.",
    "Consolidation note (2026-07-05): retracted 43 near-duplicate 'routine fallback sweep "
    "heartbeat' memories from workspace:sage.",
    "STA-47 closed done 2026-06-21. Built reusable Python client package at "
    "/host/projects/Apps/healthquery/healthquery_client/ (installable as healthquery-client).",
]


@pytest.mark.parametrize("content", EPISODIC_SAMPLES)
def test_detects_episodic_shapes(content):
    from app.services.memory_service import detect_episodic_shape

    assert detect_episodic_shape(content) is not None


@pytest.mark.parametrize("content", DURABLE_SAMPLES)
def test_leaves_durable_content_alone(content):
    from app.services.memory_service import detect_episodic_shape

    assert detect_episodic_shape(content) is None


@pytest.mark.parametrize("content", EXPIRING_SAMPLES)
def test_expiry_rule_covers_clear_per_occurrence_logs(content):
    from app.services.memory_service import detect_expiring_episodic_shape

    assert detect_expiring_episodic_shape(content) is not None


@pytest.mark.parametrize("content", DURABLE_SAMPLES + FALSE_POSITIVE_SAMPLES)
def test_expiry_rule_never_fires_on_durable_content(content):
    """Expiry deletes things months later, unattended — precision over recall."""
    from app.services.memory_service import detect_expiring_episodic_shape

    assert detect_expiring_episodic_shape(content) is None


def test_ticket_closeout_is_warned_about_but_not_expired():
    """These routinely carry a durable payload in the same record."""
    from app.services.memory_service import (
        detect_episodic_shape,
        detect_expiring_episodic_shape,
    )

    closeout = FALSE_POSITIVE_SAMPLES[3]
    assert detect_episodic_shape(closeout) is not None
    assert detect_expiring_episodic_shape(closeout) is None


def test_expiry_rule_requires_a_timestamp():
    from app.services.memory_service import detect_expiring_episodic_shape

    assert detect_expiring_episodic_shape("Routine fallback sweep heartbeat.") is None


def test_expiry_rule_requires_the_marker_up_front():
    """A durable record that mentions a tick 3 paragraphs down is not a tick."""
    from app.services.memory_service import detect_expiring_episodic_shape

    buried = (
        "Design note on the routine scheduler as of 2026-06-07. " + ("x " * 200)
        + "Each continuation tick re-reads the registry."
    )
    assert detect_expiring_episodic_shape(buried) is None


def test_episodic_write_gets_an_expiry():
    from app.services.memory_service import write_memory

    record, _ = write_memory(
        content=EPISODIC_SAMPLES[0],
        memory_class="fact",
        scope="workspace:proj",
    )
    assert record["expires_at"] is not None
    days = (
        parse_utc_datetime(record["expires_at"]) - parse_utc_datetime(record["created_at"])
    ).days
    assert 29 <= days <= 30


def test_durable_write_gets_no_expiry():
    from app.services.memory_service import write_memory

    record, _ = write_memory(
        content=DURABLE_SAMPLES[0],
        memory_class="decision",
        scope="workspace:proj",
    )
    assert record["expires_at"] is None


def test_explicit_expiry_is_never_overridden():
    from app.services.memory_service import write_memory

    record, _ = write_memory(
        content=EPISODIC_SAMPLES[0],
        memory_class="fact",
        scope="workspace:proj",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    assert record["expires_at"].startswith("2099-01-01")


def test_preferences_are_never_treated_as_episodic():
    from app.services.memory_service import write_memory

    record, _ = write_memory(
        content="the operator's heartbeat reports should always be plain text.",
        memory_class="preference",
        scope="workspace:proj",
    )
    assert record["expires_at"] is None


def test_ttl_setting_of_zero_disables_auto_expiry():
    from app.database import get_db
    from app.services.memory_service import write_memory

    with get_db() as conn:
        conn.execute(
            "INSERT INTO system_settings (key, value) VALUES ('episodic_memory_ttl_days', '0')"
        )
        conn.commit()

    record, _ = write_memory(
        content=EPISODIC_SAMPLES[0],
        memory_class="fact",
        scope="workspace:proj",
    )
    assert record["expires_at"] is None


def test_ttl_setting_is_honoured():
    from app.database import get_db
    from app.services.memory_service import write_memory

    with get_db() as conn:
        conn.execute(
            "INSERT INTO system_settings (key, value) VALUES ('episodic_memory_ttl_days', '5')"
        )
        conn.commit()

    record, _ = write_memory(
        content=EPISODIC_SAMPLES[1],
        memory_class="fact",
        scope="workspace:proj",
    )
    days = (
        parse_utc_datetime(record["expires_at"]) - parse_utc_datetime(record["created_at"])
    ).days
    assert 4 <= days <= 5


def test_auto_expiry_records_its_reason_in_provenance():
    import json

    from app.services.memory_service import build_provenance, write_memory

    record, _ = write_memory(
        content=EPISODIC_SAMPLES[2],
        memory_class="fact",
        scope="workspace:proj",
        provenance_json=build_provenance(
            actor_type="agent",
            actor_id="codex",
            channel="mcp",
            source_kind="agent_inference",
        ),
    )
    provenance = json.loads(record["provenance_json"])
    assert "fire" in provenance["auto_expiry_reason"]
    assert provenance["auto_expiry_days"] == 30
    # The original provenance must survive the merge.
    assert provenance["actor_id"] == "codex"


def test_write_without_provenance_is_left_alone():
    from app.services.memory_service import write_memory

    record, _ = write_memory(
        content=EXPIRING_SAMPLES[3],
        memory_class="fact",
        scope="workspace:proj",
    )
    assert record["provenance_json"] is None
    assert record["expires_at"] is not None


def test_a_write_does_not_claim_to_be_verified():
    """Writing a record asserts it; only a check against the world confirms it."""
    from app.services.memory_service import days_since_confirmed, write_memory

    record, _ = write_memory(
        content=DURABLE_SAMPLES[1],
        memory_class="fact",
        scope="workspace:proj",
    )
    assert record["last_confirmed_at"] is None
    # It still reads as fresh, because staleness falls back to the write date.
    assert days_since_confirmed(record) == 0


def test_explicit_confirmation_time_wins():
    from app.services.memory_service import write_memory

    record, _ = write_memory(
        content=DURABLE_SAMPLES[2],
        memory_class="fact",
        scope="workspace:proj",
        last_confirmed_at="2026-01-01T00:00:00+00:00",
    )
    assert record["last_confirmed_at"].startswith("2026-01-01")


def test_episodic_write_is_warned_about():
    from app.services.memory_service import assess_memory_write

    warnings = assess_memory_write(
        content=EPISODIC_SAMPLES[0],
        scope="workspace:proj",
        memory_class="fact",
        check_duplicates=False,
    )
    assert [w["code"] for w in warnings] == ["EPISODIC_CONTENT"]
    assert "activity_update" in warnings[0]["message"]


def test_durable_write_is_not_warned_about():
    from app.services.memory_service import assess_memory_write

    warnings = assess_memory_write(
        content=DURABLE_SAMPLES[0],
        scope="workspace:proj",
        memory_class="decision",
        check_duplicates=False,
    )
    assert warnings == []


def test_duplicate_check_is_silent_without_vector_search():
    """No embeddings means no reliable similarity, so it must not guess."""
    from app.services.memory_service import find_near_duplicates, write_memory

    write_memory(
        content=DURABLE_SAMPLES[0],
        memory_class="decision",
        scope="workspace:proj",
    )
    assert find_near_duplicates(DURABLE_SAMPLES[0], "workspace:proj") == []
